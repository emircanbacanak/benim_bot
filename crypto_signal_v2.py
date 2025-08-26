#!/usr/bin/env python3
import sys
import asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import pandas as pd
import ta
from datetime import datetime, timedelta
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import json
import aiohttp
from aiohttp import web
from dotenv import load_dotenv
import os
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from decimal import Decimal, ROUND_DOWN, getcontext
from binance.client import Client
import re

load_dotenv()

# Kripto özel ayarları (backtest sonuçlarına göre optimize edildi)
CRYPTO_SETTINGS = {
    "SOLUSDT": {
        "timeframes": ["1h", "2h"],           # 1h+2h kombinasyonu
        "tp_percent": 15.0,                   # %15.0 TP
        "sl_percent": 7.5,                    # %7.5 SL
        "leverage": 10
    },
    "AVAXUSDT": {
        "timeframes": ["30m", "1h"],          # 30m+1h kombinasyonu
        "tp_percent": 5.0,                    # %5.0 TP
        "sl_percent": 2.5,                    # %2.5 SL
        "leverage": 10
    },
    "ETHUSDT": {
        "timeframes": ["30m", "1h"],          # 30m+1h kombinasyonu
        "tp_percent": 12.0,                   # %12.0 TP
        "sl_percent": 6.0,                    # %6.0 SL
        "leverage": 10
    },
    "ADAUSDT": {
        "timeframes": ["30m", "1h"],          # 30m+1h kombinasyonu
        "tp_percent": 20.0,                   # %20.0 TP
        "sl_percent": 10.0,                   # %10.0 SL
        "leverage": 10
    }
}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
MONGODB_DB = os.getenv("MONGODB_DB", "crypto_signal_bot")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "allowed_users")

BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))
ADMIN_USERS = set()

mongo_client = None
mongo_db = None
mongo_collection = None

client = Client()

def validate_user_command(update, require_admin=False, require_owner=False):
    """Kullanıcı komut yetkisini kontrol eder"""
    if not update.effective_user:
        return None, False
    
    user_id = update.effective_user.id
    
    if require_owner and user_id != BOT_OWNER_ID:
        return user_id, False
    
    if require_admin and not is_admin(user_id):
        return user_id, False
    
    return user_id, True

def validate_command_args(update, context, expected_args=1):
    """Komut argümanlarını kontrol eder"""
    if not context.args or len(context.args) < expected_args:
        # Komut adını update.message.text'ten al
        command_name = "komut"
        if update and update.message and update.message.text:
            text = update.message.text
            if text.startswith('/'):
                command_name = text.split()[0][1:]  # /adduser -> adduser
        return False, f"❌ Kullanım: /{command_name} {' '.join(['<arg>'] * expected_args)}"
    return True, None

def validate_user_id(user_id_str):
    """User ID'yi doğrular ve döndürür"""
    try:
        user_id = int(user_id_str)
        return True, user_id
    except ValueError:
        return False, "❌ Geçersiz user_id. Lütfen sayısal bir değer girin."

async def send_command_response(update, message, parse_mode='Markdown'):
    """Komut yanıtını gönderir"""
    try:
        await update.message.reply_text(message, parse_mode=parse_mode)
    except Exception as e:
        print(f"❌ Markdown formatında gönderilemedi: {e}")
        # HTML formatında dene
        try:
            # Markdown'ı HTML'e dönüştür
            html_message = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', message)
            await update.message.reply_text(html_message, parse_mode='HTML')
        except Exception as e2:
            print(f"❌ HTML formatında da gönderilemedi: {e2}")
            # Son çare olarak düz metin olarak gönder
            await update.message.reply_text(message, parse_mode=None)

async def api_request_with_retry(session, url, ssl=False, max_retries=None):
    if max_retries is None:
        max_retries = 3  # API_RETRY_ATTEMPTS

    retry_delays = [1, 3, 5]  # API_RETRY_DELAYS

    for attempt in range(max_retries):
        try:
            async with session.get(url, ssl=ssl) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:  # Rate limit
                    delay = retry_delays[min(attempt, len(retry_delays)-1)]
                    print(f"⚠️ Rate limit (429), {delay} saniye bekleniyor... (Deneme {attempt+1}/{max_retries})")
                    await asyncio.sleep(delay)
                    continue
                else:
                    print(f"⚠️ API hatası: {resp.status}, Deneme {attempt+1}/{max_retries}")
                    if attempt < max_retries - 1:
                        delay = retry_delays[min(attempt, len(retry_delays)-1)]
                        await asyncio.sleep(delay)
                        continue
                    else:
                        raise Exception(f"API hatası: {resp.status}")

        except asyncio.TimeoutError:
            print(f"⚠️ Timeout hatası, Deneme {attempt+1}/{max_retries}")
            if attempt < max_retries - 1:
                delay = retry_delays[min(attempt, len(retry_delays)-1)]
                await asyncio.sleep(delay)
                continue
            else:
                raise Exception("API timeout hatası")

        except Exception as e:
            print(f"⚠️ API isteği hatası: {e}, Deneme {attempt+1}/{max_retries}")
            if attempt < max_retries - 1:
                delay = retry_delays[min(attempt, len(retry_delays)-1)]
                await asyncio.sleep(delay)
                continue
            else:
                raise e

    raise Exception(f"API isteği {max_retries} denemeden sonra başarısız")

def save_data_to_db(doc_id, data, collection_name="data"):
    """Genel veri kaydetme fonksiyonu (upsert)."""
    global mongo_collection
    if mongo_collection is None:
        return False
    try:
        mongo_collection.update_one(
            {"_id": doc_id},
            {"$set": {"data": data, "updated_at": str(datetime.now())}},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"❌ {collection_name} DB kaydı hatası: {e}")
        return False

def load_data_from_db(doc_id, default_value=None):
    """Genel veri okuma fonksiyonu."""
    global mongo_collection
    if mongo_collection is None:
        return default_value
    try:
        doc = mongo_collection.find_one({"_id": doc_id})
        if doc:
            # Eğer "data" alanı varsa onu döndür, yoksa tüm dokümanı döndür (geriye uyumluluk için)
            if "data" in doc:
                return doc["data"]
            else:
                # "data" alanı yoksa, tüm dokümanı döndür (geriye uyumluluk için)
                return doc
    except Exception as e:
        print(f"❌ {doc_id} DB okuma hatası: {e}")
    return default_value

def check_klines_for_trigger(signal, klines):
    try:
        signal_type = signal.get('type', 'ALIŞ')
        symbol = signal.get('symbol', 'UNKNOWN')
        
        # Hedef ve stop fiyatlarını al
        if 'target_price' in signal and 'stop_loss' in signal:
            target_price = float(str(signal['target_price']).replace('$', '').replace(',', ''))
            stop_loss_price = float(str(signal['stop_loss']).replace('$', '').replace(',', ''))
        else:
            # Pozisyon verilerinden al
            target_price = float(str(signal.get('target', 0)).replace('$', '').replace(',', ''))
            stop_loss_price = float(str(signal.get('stop', 0)).replace('$', '').replace(',', ''))
        
        if target_price <= 0 or stop_loss_price <= 0:
            print(f"⚠️ {symbol} - Geçersiz hedef/stop fiyatları: TP={target_price}, SL={stop_loss_price}")
            return False, None, None
        
        if not klines:
            print(f"⚠️ {symbol} - Mum verisi boş")
            return False, None, None
        
        # Mum verilerini DataFrame'e dönüştür
        if isinstance(klines, list) and len(klines) > 0:
            if len(klines[0]) >= 6:  # OHLCV formatı
                df = pd.DataFrame(klines, columns=['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_asset_volume', 'number_of_trades', 'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
                df = df[['open', 'high', 'low', 'close']].astype(float)
            else:
                print(f"⚠️ {signal.get('symbol', 'UNKNOWN')} - Geçersiz mum veri formatı")
                return False, None, None
        else:
            print(f"⚠️ {signal.get('symbol', 'UNKNOWN')} - Mum verisi bulunamadı")
            return False, None, None
        
        symbol = signal.get('symbol', 'UNKNOWN')
        
        for index, row in df.iterrows():
            high = float(row['high'])
            low = float(row['low'])
            
                    # Minimum tetikleme farkı (sıfır bölme ve yanlış tetiklemeyi önler)
        min_trigger_diff = 0.001  # %0.1 minimum fark
        
        # ALIŞ sinyali kontrolü (long pozisyon)
        if signal_type == "ALIŞ" or signal_type == "ALIS":
            # Önce hedef kontrolü (kural olarak kar alma öncelikli)
            if high >= target_price and (high - target_price) >= (target_price * min_trigger_diff):
                print(f"✅ {symbol} - TP tetiklendi! Mum: High={high:.6f}, TP={target_price:.6f}")
                return True, "take_profit", high
            # Sonra stop-loss kontrolü - eşit veya geçmişse
            if low <= stop_loss_price and (stop_loss_price - low) >= (stop_loss_price * min_trigger_diff):
                print(f"❌ {symbol} - SL tetiklendi! Mum: Low={low:.6f}, SL={stop_loss_price:.6f}")
                return True, "stop_loss", low
                
        # SATIŞ sinyali kontrolü (short pozisyon)
        elif signal_type == "SATIŞ" or signal_type == "SATIS":
            # Önce hedef kontrolü
            if low <= target_price and (target_price - low) >= (target_price * min_trigger_diff):
                print(f"✅ {symbol} - TP tetiklendi! Mum: Low={low:.6f}, TP={target_price:.6f}")
                return True, "take_profit", low
            # Sonra stop-loss kontrolü - eşit veya geçmişse
            if high >= stop_loss_price and (high - stop_loss_price) >= (stop_loss_price * min_trigger_diff):
                print(f"❌ {symbol} - SL tetiklendi! Mum: High={high:.6f}, SL={stop_loss_price:.6f}")
                return True, "stop_loss", high

        # Hiçbir tetikleme yoksa, false döner ve son mumu döndürür
        final_price = float(df['close'].iloc[-1]) if not df.empty else None
        return False, None, final_price
        
    except Exception as e:
        print(f"❌ check_klines_for_trigger hatası ({signal.get('symbol', 'UNKNOWN')}): {e}")
        return False, None, None

def save_stats_to_db(stats):
    """İstatistik sözlüğünü MongoDB'ye kaydeder."""
    return save_data_to_db("bot_stats", stats, "Stats")

def load_stats_from_db():
    """MongoDB'den son istatistik sözlüğünü döndürür."""
    return load_data_from_db("bot_stats", {})

def update_stats_atomic(updates):
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, atomik güncelleme yapılamadı")
                return False
        
        # Atomik $inc operatörü ile güncelleme
        update_data = {}
        for key, value in updates.items():
            update_data[f"data.{key}"] = value
        
        result = mongo_collection.update_one(
            {"_id": "bot_stats"},
            {"$inc": update_data, "$set": {"data.last_updated": str(datetime.now())}},
            upsert=True
        )
        
        if result.modified_count > 0 or result.upserted_id:
            print(f"✅ İstatistikler atomik olarak güncellendi: {updates}")
            return True
        else:
            print(f"⚠️ İstatistik güncellemesi yapılamadı: {updates}")
            return False
            
    except Exception as e:
        print(f"❌ Atomik istatistik güncelleme hatası: {e}")
        return False

def update_position_status_atomic(symbol, status, additional_data=None):
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, pozisyon durumu güncellenemedi")
                return False
        
        # Önce dokümanın var olup olmadığını kontrol et
        existing_doc = mongo_collection.find_one({"_id": f"active_signal_{symbol}"})
        
        if existing_doc:
            # Doküman varsa, data alanı var mı kontrol et
            if "data" in existing_doc:
                # Data alanı varsa normal güncelleme
                update_data = {"$set": {"data.status": status, "data.last_updated": str(datetime.now())}}
                
                if additional_data:
                    for key, value in additional_data.items():
                        update_data["$set"][f"data.{key}"] = value
                
                result = mongo_collection.update_one(
                    {"_id": f"active_signal_{symbol}"},
                    update_data,
                    upsert=False
                )
            else:
                # Data alanı yoksa, önce onu oluştur
                update_data = {"$set": {"data": {"status": status, "last_updated": str(datetime.now())}}}
                
                if additional_data:
                    for key, value in additional_data.items():
                        update_data["$set"][f"data.{key}"] = value
                
                result = mongo_collection.update_one(
                    {"_id": f"active_signal_{symbol}"},
                    update_data,
                    upsert=False
                )
        else:
            # Doküman yoksa, yeni oluştur
            new_doc = {
                "_id": f"active_signal_{symbol}",
                "data": {
                    "status": status,
                    "last_updated": str(datetime.now())
                }
            }
            
            if additional_data:
                for key, value in additional_data.items():
                    new_doc["data"][key] = value
            
            result = mongo_collection.insert_one(new_doc)
        
        # insert_one için upserted_id, update_one için modified_count kontrol et
        if hasattr(result, 'modified_count') and result.modified_count > 0:
            print(f"✅ {symbol} pozisyon durumu güncellendi: {status}")
            return True
        elif hasattr(result, 'upserted_id') and result.upserted_id:
            print(f"✅ {symbol} pozisyon durumu oluşturuldu: {status}")
            return True
        else:
            print(f"⚠️ {symbol} pozisyon durumu güncellenemedi: {status} (Result: {result})")
            return False
            
    except Exception as e:
        print(f"❌ Pozisyon durumu güncelleme hatası ({symbol}): {e}")
        return False

def save_active_signals_to_db(active_signals):
    """Aktif sinyalleri MongoDB'ye kaydeder."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, aktif sinyaller kaydedilemedi")
                return False
        
        # Eğer boş sözlük ise, tüm aktif sinyal dokümanlarını sil
        if not active_signals:
            try:
                delete_result = mongo_collection.delete_many({"_id": {"$regex": "^active_signal_"}})
                deleted_count = getattr(delete_result, "deleted_count", 0)
                print(f"🧹 Boş aktif sinyal listesi için {deleted_count} doküman silindi")
                return True
            except Exception as e:
                print(f"❌ Boş aktif sinyal temizleme hatası: {e}")
                return False
        
        # Her aktif sinyali ayrı doküman olarak kaydet
        for symbol, signal in active_signals.items():
            signal_doc = {
                "_id": f"active_signal_{symbol}",
                "symbol": signal["symbol"],
                "type": signal["type"],
                "entry_price": signal["entry_price"],
                "entry_price_float": signal["entry_price_float"],
                "target_price": signal["target_price"],
                "stop_loss": signal["stop_loss"],
                "signals": signal["signals"],
                "leverage": signal["leverage"],
                "signal_time": signal["signal_time"],
                "current_price": signal["current_price"],
                "current_price_float": signal["current_price_float"],
                "last_update": signal["last_update"],
                "max_price": signal.get("max_price", 0),  # Max fiyat
                "min_price": signal.get("min_price", 0),  # Min fiyat
                "status": signal.get("status", "active"),  # Mevcut durumu kullan, yoksa "active"
                "saved_at": str(datetime.now())
            }
            
            # Doğrudan MongoDB'ye kaydet (save_data_to_db kullanma)
            try:
                mongo_collection.update_one(
                    {"_id": f"active_signal_{symbol}"},
                    {"$set": signal_doc},
                    upsert=True
                )
            except Exception as e:
                print(f"❌ {symbol} aktif sinyali kaydedilemedi: {e}")
                return False
        
        print(f"✅ MongoDB'ye {len(active_signals)} aktif sinyal kaydedildi")
        return True
    except Exception as e:
        print(f"❌ MongoDB'ye aktif sinyaller kaydedilirken hata: {e}")
        return False

def load_active_signals_from_db():
    """MongoDB'den aktif sinyalleri döndürür."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, aktif sinyaller yüklenemedi")
                return {}
        
        result = {}
        docs = mongo_collection.find({"_id": {"$regex": "^active_signal_"}})
        
        for doc in docs:
            # Artık veri doğrudan dokümanda, data alanında değil
            if "symbol" not in doc:
                continue
            symbol = doc["symbol"]
            result[symbol] = {
                "symbol": doc.get("symbol", symbol),
                "type": doc.get("type", "ALIŞ"),
                "entry_price": doc.get("entry_price", "0"),
                "entry_price_float": doc.get("entry_price_float", 0.0),
                "target_price": doc.get("target_price", "0"),
                "stop_loss": doc.get("stop_loss", "0"),
                "signals": doc.get("signals", {}),
                "leverage": doc.get("leverage", 10),
                "signal_time": doc.get("signal_time", ""),
                "current_price": doc.get("current_price", "0"),
                "current_price_float": doc.get("current_price_float", 0.0),
                "last_update": doc.get("last_update", ""),
                "max_price": doc.get("max_price", 0),  # Max fiyat
                "min_price": doc.get("min_price", 0),  # Min fiyat
                "status": doc.get("status", "active")  # Varsayılan durum "active"
            }
        return result
    except Exception as e:
        print(f"❌ MongoDB'den aktif sinyaller yüklenirken hata: {e}")
        return {}

ALLOWED_USERS = set()

def connect_mongodb():
    """MongoDB bağlantısını kur"""
    global mongo_client, mongo_db, mongo_collection
    try:
        mongo_client = MongoClient(MONGODB_URI, 
                                  serverSelectionTimeoutMS=30000,
                                  connectTimeoutMS=30000,
                                  socketTimeoutMS=30000,
                                  maxPoolSize=10)
        mongo_client.admin.command('ping')
        mongo_db = mongo_client[MONGODB_DB]
        mongo_collection = mongo_db[MONGODB_COLLECTION]
        return True
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        print(f"❌ MongoDB bağlantı hatası: {e}")
        return False
    except Exception as e:
        print(f"❌ MongoDB bağlantı hatası: {e}")
        return False

def ensure_mongodb_connection():
    """MongoDB bağlantısının aktif olduğundan emin ol, değilse yeniden bağlan"""
    global mongo_collection
    try:
        if mongo_collection is None:
            return connect_mongodb()
        
        mongo_client.admin.command('ping')
        return True
    except Exception as e:
        print(f"⚠️ MongoDB bağlantısı koptu, yeniden bağlanılıyor: {e}")
        return connect_mongodb()

def load_allowed_users():
    """İzin verilen kullanıcıları ve admin bilgilerini MongoDB'den yükle"""
    global ALLOWED_USERS, ADMIN_USERS
    try:
        if not connect_mongodb():
            print("⚠️ MongoDB bağlantısı kurulamadı, boş liste ile başlatılıyor")
            ALLOWED_USERS = set()
            ADMIN_USERS = set()
            return
        
        users_data = load_data_from_db("allowed_users")
        if users_data and 'user_ids' in users_data:
            ALLOWED_USERS = set(users_data['user_ids'])
        else:
            print("ℹ️ MongoDB'de izin verilen kullanıcı bulunamadı, boş liste ile başlatılıyor")
            ALLOWED_USERS = set()
        
        # Admin grupları kaldırıldı - sadece özel mesajlar destekleniyor
        
        admin_users_data = load_data_from_db("admin_users")
        if admin_users_data and 'admin_ids' in admin_users_data:
            ADMIN_USERS = set(admin_users_data['admin_ids'])
        else:
            print("ℹ️ MongoDB'de admin kullanıcı bulunamadı, boş liste ile başlatılıyor")
            ADMIN_USERS = set()
    except Exception as e:
        print(f"❌ MongoDB'den veriler yüklenirken hata: {e}")
        ALLOWED_USERS = set()
        ADMIN_USERS = set()
    
    # Bot sahibini otomatik olarak ekle
    if BOT_OWNER_ID not in ALLOWED_USERS:
        ALLOWED_USERS.add(BOT_OWNER_ID)
        print(f"✅ Bot sahibi {BOT_OWNER_ID} otomatik olarak izin verilen kullanıcılara eklendi")
    
    # Bot sahibini admin olarak da ekle
    if BOT_OWNER_ID not in ADMIN_USERS:
        ADMIN_USERS.add(BOT_OWNER_ID)
        print(f"✅ Bot sahibi {BOT_OWNER_ID} otomatik olarak admin listesine eklendi")
    
    # Değişiklikleri veritabanına kaydet
    if BOT_OWNER_ID not in users_data.get('user_ids', []) if users_data else True:
        save_allowed_users()
        print(f"💾 Bot sahibi {BOT_OWNER_ID} veritabanına kaydedildi")
    
    if BOT_OWNER_ID not in admin_users_data.get('admin_ids', []) if admin_users_data else True:
        save_admin_users()
        print(f"💾 Bot sahibi {BOT_OWNER_ID} admin olarak veritabanına kaydedildi")

def save_allowed_users():
    """İzin verilen kullanıcıları MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, kullanıcılar kaydedilemedi")
                return False
        
        user_data = {
            "user_ids": list(ALLOWED_USERS),
            "last_updated": str(datetime.now()),
            "count": len(ALLOWED_USERS)
        }
        
        if save_data_to_db("allowed_users", user_data, "İzin Verilen Kullanıcılar"):
            return True
        return False
    except Exception as e:
        print(f"❌ MongoDB'ye kullanıcılar kaydedilirken hata: {e}")
        return False

async def set_cooldown_to_db(cooldown_delta: timedelta):
    """Cooldown bitiş zamanını veritabanına kaydeder."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, cooldown kaydedilemedi")
                return False
        
        cooldown_until = datetime.now() + cooldown_delta
        mongo_collection.update_one(
            {"_id": "cooldown"},
            {"$set": {"until": cooldown_until, "timestamp": datetime.now()}},
            upsert=True
        )
        print(f"⏳ Cooldown süresi ayarlandı: {cooldown_until}")
        return True
    except Exception as e:
        print(f"❌ Cooldown veritabanına kaydedilirken hata: {e}")
        return False

async def check_cooldown_status():
    """Cooldown durumunu veritabanından kontrol eder ve döner."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return None
        
        doc = mongo_collection.find_one({"_id": "cooldown"})
        if doc and doc.get("until") and doc["until"] > datetime.now():
            return doc["until"]
        
        return None  # Cooldown yok
    except Exception as e:
        print(f"❌ Cooldown durumu kontrol edilirken hata: {e}")
        return None

async def clear_cooldown_status():
    """Cooldown durumunu veritabanından temizler."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, cooldown temizlenemedi")
                return False
        
        mongo_collection.delete_one({"_id": "cooldown"})
        return True
    except Exception as e:
        print(f"❌ Cooldown durumu temizlenirken hata: {e}")
        return False

async def set_signal_cooldown_to_db(symbols, cooldown_delta: timedelta):
    """Belirtilen sembolleri cooldown'a ekler."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, sinyal cooldown kaydedilemedi")
                return False
        
        cooldown_until = datetime.now() + cooldown_delta
        
        for symbol in symbols:
            mongo_collection.update_one(
                {"_id": f"signal_cooldown_{symbol}"},
                {"$set": {"until": cooldown_until, "timestamp": datetime.now()}},
                upsert=True
            )
        
        print(f"⏳ {len(symbols)} sinyal cooldown'a eklendi: {', '.join(symbols)}")
        return True
    except Exception as e:
        print(f"❌ Sinyal cooldown veritabanına kaydedilirken hata: {e}")
        return False

async def check_signal_cooldown(symbol):
    """Belirli bir sembolün cooldown durumunu kontrol eder."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return False
        
        doc = mongo_collection.find_one({"_id": f"signal_cooldown_{symbol}"})
        if doc and doc.get("until") and doc["until"] > datetime.now():
            return True  # Cooldown'da
        
        return False  # Cooldown yok
    except Exception as e:
        print(f"❌ Sinyal cooldown durumu kontrol edilirken hata: {e}")
        return False

async def clear_signal_cooldown(symbol):
    """Belirli bir sembolün cooldown durumunu temizler."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return False
        
        mongo_collection.delete_one({"_id": f"signal_cooldown_{symbol}"})
        return True
    except Exception as e:
        print(f"❌ Sinyal cooldown temizlenirken hata: {e}")
        return False

async def get_expired_cooldown_signals():
    """Cooldown süresi biten sinyalleri döndürür ve temizler."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return []
        
        expired_signals = []
        current_time = datetime.now()
        
        # Süresi biten cooldown'ları bul
        expired_docs = mongo_collection.find({
            "_id": {"$regex": "^signal_cooldown_"},
            "until": {"$lte": current_time}
        })
        
        for doc in expired_docs:
            symbol = doc["_id"].replace("signal_cooldown_", "")
            expired_signals.append(symbol)
            # Süresi biten cooldown'ı sil
            mongo_collection.delete_one({"_id": doc["_id"]})
        
        if expired_signals:
            print(f"🔄 {len(expired_signals)} sinyal cooldown süresi bitti: {', '.join(expired_signals)}")
        
        return expired_signals
    except Exception as e:
        print(f"❌ Süresi biten cooldown sinyalleri alınırken hata: {e}")
        return []

async def get_volumes_for_symbols(symbols):
    """Belirtilen semboller için hacim verilerini Binance'den çeker."""
    try:
        volumes = {}
        for symbol in symbols:
            try:
                ticker_data = client.futures_ticker(symbol=symbol)
                
                # API bazen liste döndürüyor, bazen dict
                if isinstance(ticker_data, list):
                    if len(ticker_data) == 0:
                        volumes[symbol] = 0
                        continue
                    ticker = ticker_data[0]  # İlk elementi al
                else:
                    ticker = ticker_data
                
                if ticker and isinstance(ticker, dict) and 'quoteVolume' in ticker:
                    volumes[symbol] = float(ticker['quoteVolume'])
                else:
                    volumes[symbol] = 0
                    
            except Exception as e:
                print(f"⚠️ {symbol} hacim verisi alınamadı: {e}")
                volumes[symbol] = 0
        
        return volumes
    except Exception as e:
        print(f"❌ Hacim verileri alınırken hata: {e}")
        return {symbol: 0 for symbol in symbols}

# save_admin_groups fonksiyonu kaldırıldı - artık grup desteği yok

def save_admin_users():
    """Admin kullanıcılarını MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, admin kullanıcıları kaydedilemedi")
                return False
        
        admin_data = {
            "admin_ids": list(ADMIN_USERS),
            "last_updated": str(datetime.now()),
            "count": len(ADMIN_USERS)
        }
        
        if save_data_to_db("admin_users", admin_data, "Admin Kullanıcıları"):
            print(f"✅ MongoDB'ye {len(ADMIN_USERS)} admin kullanıcı kaydedildi")
            return True
        return False
    except Exception as e:
        print(f"❌ MongoDB'ye admin kullanıcıları kaydedilirken hata: {e}")
        return False

def close_mongodb():
    """MongoDB bağlantısını kapat"""
    global mongo_client
    if mongo_client:
        try:
            mongo_client.close()
            print("✅ MongoDB bağlantısı kapatıldı")
        except Exception as e:
            print(f"⚠️ MongoDB bağlantısı kapatılırken hata: {e}")

def save_positions_to_db(positions):
    """Pozisyonları MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, pozisyonlar kaydedilemedi")
                return False
                
        for symbol, position in positions.items():
            doc_id = f"position_{symbol}"
            
            if not position or not isinstance(position, dict):
                print(f"⚠️ {symbol} - Geçersiz pozisyon verisi, atlanıyor")
                continue
                
            required_fields = ['type', 'target', 'stop', 'open_price', 'leverage']
            missing_fields = [field for field in required_fields if field not in position]
            
            if missing_fields:
                print(f"⚠️ {symbol} - Eksik alanlar: {missing_fields}, pozisyon atlanıyor")
                continue
            
            try:
                open_price = float(position['open_price'])
                target_price = float(position['target'])
                stop_price = float(position['stop'])
                
                if open_price <= 0 or target_price <= 0 or stop_price <= 0:
                    print(f"⚠️ {symbol} - Geçersiz fiyat değerleri, pozisyon atlanıyor")
                    print(f"   Giriş: {open_price}, Hedef: {target_price}, Stop: {stop_price}")
                    continue
                    
            except (ValueError, TypeError) as e:
                print(f"⚠️ {symbol} - Fiyat dönüşüm hatası: {e}, pozisyon atlanıyor")
                continue

            # Pozisyon verilerini data alanında kaydet (tutarlı yapı için)
            result = mongo_collection.update_one(
                {"_id": doc_id},
                {
                    "$set": {
                        "symbol": symbol,
                        "data": position,  # TÜM POZİSYON VERİSİ BURAYA GELECEK
                        "timestamp": datetime.now()
                    }
                },
                upsert=True
            )
            
            if result.modified_count > 0 or result.upserted_id:
                print(f"✅ {symbol} pozisyonu güncellendi/eklendi")
                
                # Pozisyon kaydedildikten sonra active_signal dokümanını da oluştur
                try:
                    # Pozisyon verilerinden active_signal dokümanı oluştur
                    active_signal_doc = {
                        "_id": f"active_signal_{symbol}",
                        "symbol": symbol,
                        "type": position.get("type", "ALIŞ"),
                        "entry_price": format_price(position.get("open_price", 0), position.get("open_price", 0)),
                        "entry_price_float": position.get("open_price", 0),
                        "target_price": format_price(position.get("target", 0), position.get("open_price", 0)),
                        "stop_loss": format_price(position.get("stop", 0), position.get("open_price", 0)),
                        "signals": position.get("signals", {}),
                        "leverage": position.get("leverage", 10),
                        "signal_time": position.get("entry_time", datetime.now().strftime('%Y-%m-%d %H:%M')),
                        "current_price": format_price(position.get("open_price", 0), position.get("open_price", 0)),
                        "current_price_float": position.get("open_price", 0),
                        "last_update": str(datetime.now()),
                        "status": "active",
                        "saved_at": str(datetime.now())
                    }
                    
                    # Active signal dokümanını kaydet
                    mongo_collection.update_one(
                        {"_id": f"active_signal_{symbol}"},
                        {"$set": active_signal_doc},
                        upsert=True
                    )
                    print(f"✅ {symbol} active_signal dokümanı oluşturuldu")
                    
                except Exception as e:
                    print(f"⚠️ {symbol} active_signal dokümanı oluşturulurken hata: {e}")
            else:
                print(f"⚠️ {symbol} pozisyonu güncellenemedi")
        
        print(f"✅ {len(positions)} pozisyon MongoDB'ye kaydedildi")
        
        # Pozisyon durumlarını güncelle - artık active_signal dokümanları zaten oluşturuldu
        for symbol in positions.keys():
            try:
                # Durumu "active" olarak güncelle
                update_position_status_atomic(symbol, "active")
            except Exception as e:
                print(f"⚠️ {symbol} pozisyon durumu güncellenirken hata: {e}")
        
        return True
    except Exception as e:
        print(f"❌ Pozisyonlar MongoDB'ye kaydedilirken hata: {e}")
        return False

def migrate_old_position_format():
    """Eski pozisyon verilerini yeni formata dönüştürür"""
    try:
        if mongo_collection is None:
            return False
        
        # Migration fonksiyonu artık gerekli değil - kaldırıldı
        pass
        
        return True
    except Exception as e:
        print(f"❌ Pozisyon formatı dönüştürülürken hata: {e}")
        return False

def load_positions_from_db():
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, pozisyonlar yüklenemedi")
                return {}
        
        positions = {}
        docs = mongo_collection.find({"_id": {"$regex": "^position_"}})
        
        for doc in docs:
            symbol = doc["_id"].replace("position_", "")
            position_data = doc.get('data', doc)
            
            if not position_data or not isinstance(position_data, dict):
                print(f"⚠️ {symbol} - Geçersiz pozisyon verisi formatı, atlanıyor")
                continue
            
            required_fields = ['type', 'target', 'stop', 'open_price', 'leverage']
            missing_fields = [field for field in required_fields if field not in position_data]
            
            if missing_fields:
                print(f"⚠️ {symbol} - Eksik alanlar: {missing_fields}, pozisyon atlanıyor")
                continue
            
            try:
                open_price = float(position_data['open_price'])
                target_price = float(position_data['target'])
                stop_price = float(position_data['stop'])
                
                if open_price <= 0 or target_price <= 0 or stop_price <= 0:
                    print(f"⚠️ {symbol} - Geçersiz fiyat değerleri, pozisyon atlanıyor")
                    print(f"   Giriş: {open_price}, Hedef: {target_price}, Stop: {stop_price}")
                    continue
                    
            except (ValueError, TypeError) as e:
                print(f"⚠️ {symbol} - Fiyat dönüşüm hatası: {e}, pozisyon atlanıyor")
                continue
            
            positions[symbol] = position_data
        
        return positions
    except Exception as e:
        print(f"❌ MongoDB'den pozisyonlar yüklenirken hata: {e}")
        return {}

def load_position_from_db(symbol):
    """MongoDB'den tek pozisyon yükler."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, pozisyon yüklenemedi")
                return None
        
        doc = mongo_collection.find_one({"_id": f"position_{symbol}"})
        if doc:
            # Veriyi hem yeni (data anahtarı) hem de eski yapıdan (doğrudan doküman) almaya çalış
            position_data = doc.get('data', doc)
            
            if "open_price" in position_data:
                try:
                    open_price_raw = position_data.get("open_price", 0)
                    target_price_raw = position_data.get("target", 0)
                    stop_loss_raw = position_data.get("stop", 0)
                    
                    open_price = float(open_price_raw) if open_price_raw is not None else 0.0
                    target_price = float(target_price_raw) if target_price_raw is not None else 0.0
                    stop_loss = float(stop_loss_raw) if stop_loss_raw is not None else 0.0

                    if open_price <= 0 or target_price <= 0 or stop_loss <= 0:
                        print(f"⚠️ {symbol} - Geçersiz pozisyon fiyatları tespit edildi")
                        print(f"   Giriş: {open_price}, Hedef: {target_price}, Stop: {stop_loss}")
                        print(f"   ⚠️ Pozisyon verisi yüklenemedi, ancak silinmedi")
                        return None
                    
                    validated_data = position_data.copy()
                    validated_data["open_price"] = open_price
                    validated_data["target"] = target_price
                    validated_data["stop"] = stop_loss
                    validated_data["leverage"] = int(position_data.get("leverage", 10))
                    return validated_data
                    
                except (ValueError, TypeError) as e:
                    print(f"❌ {symbol} - Pozisyon verisi dönüşüm hatası: {e}")
                    print(f"   Raw doc: {doc}")
                    print(f"   ⚠️ Pozisyon verisi yüklenemedi, ancak silinmedi")
                    return None
        
        # Aktif sinyal dokümanından veri okuma kısmını kaldır - artık pozisyon dokümanlarından okuyoruz
        # Bu kısım kaldırıldı çünkü pozisyon verileri artık doğrudan position_ dokümanlarında
        print(f"❌ {symbol} için hiçbir pozisyon verisi bulunamadı!")
        return None
        
    except Exception as e:
        print(f"❌ MongoDB'den {symbol} pozisyonu yüklenirken hata: {e}")
        return None

def load_stop_cooldown_from_db():
    """MongoDB'den stop cooldown verilerini yükler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, stop cooldown yüklenemedi")
                return {}
        
        stop_cooldown = {}
        docs = mongo_collection.find({"_id": {"$regex": "^stop_cooldown_"}})
        
        for doc in docs:
            symbol = doc["_id"].replace("stop_cooldown_", "")
            stop_cooldown[symbol] = doc["data"]
        
        print(f"📊 MongoDB'den {len(stop_cooldown)} stop cooldown yüklendi")
        return stop_cooldown
    except Exception as e:
        print(f"❌ MongoDB'den stop cooldown yüklenirken hata: {e}")
        return {}

def save_previous_signals_to_db(previous_signals):
    """Önceki sinyalleri MongoDB'ye kaydet (sadece ilk çalıştırmada)"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, önceki sinyaller kaydedilemedi")
                return False
        
        existing_doc = mongo_collection.find_one({"_id": "previous_signals_initialized"})
        if existing_doc:
            print("ℹ️ Önceki sinyaller zaten kaydedilmiş, tekrar kaydedilmiyor")
            return True
        
        for symbol, signals in previous_signals.items():
            signal_doc = {
                "_id": f"previous_signal_{symbol}",
                "symbol": symbol,
                "signals": signals,
                "saved_time": str(datetime.now())
            }
            
            if not save_data_to_db(f"previous_signal_{symbol}", signal_doc, "Önceki Sinyal"):
                return False
        
        if not save_data_to_db("previous_signals_initialized", {"initialized": True, "initialized_time": str(datetime.now())}, "İlk Kayıt"):
            return False
        
        print(f"✅ MongoDB'ye {len(previous_signals)} önceki sinyal kaydedildi (ilk çalıştırma)")
        return True
    except Exception as e:
        print(f"❌ MongoDB'ye önceki sinyaller kaydedilirken hata: {e}")
        return False

def load_previous_signals_from_db():
    try:
        def transform_signal(doc):
            symbol = doc["_id"].replace("previous_signal_", "")
            if "signals" in doc:
                return {symbol: doc["signals"]}
            else:
                return {symbol: doc}
        
        return load_data_by_pattern("^previous_signal_", "signals", "önceki sinyal", transform_signal)
    except Exception as e:
        print(f"❌ MongoDB'den önceki sinyaller yüklenirken hata: {e}")
        return {}

def is_first_run():
    """İlk çalıştırma mı kontrol et"""
    def check_first_run():
        # Önceki sinyallerin kaydedilip kaydedilmediğini kontrol et
        existing_doc = mongo_collection.find_one({"_id": "previous_signals_initialized"})
        if existing_doc is None:
            return True  # İlk çalıştırma
        
        # Pozisyonların varlığını da kontrol et
        position_count = mongo_collection.count_documents({"_id": {"$regex": "^position_"}})
        if position_count > 0:
            print(f"📊 MongoDB'de {position_count} aktif pozisyon bulundu, yeniden başlatma olarak algılanıyor")
            return False  # Yeniden başlatma
        
        # Önceki sinyallerin varlığını kontrol et
        signal_count = mongo_collection.count_documents({"_id": {"$regex": "^previous_signal_"}})
        if signal_count > 0:
            print(f"📊 MongoDB'de {signal_count} önceki sinyal bulundu, yeniden başlatma olarak algılanıyor")
            return False  # Yeniden başlatma
        
        return True  # İlk çalıştırma
    
    return safe_mongodb_operation(check_first_run, "İlk çalıştırma kontrolü", True)

def update_previous_signal_in_db(symbol, signals):
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return False
        
        signal_doc = {
            "_id": f"previous_signal_{symbol}",
            "symbol": symbol,
            "signals": signals,
            "updated_time": str(datetime.now())
        }
        
        if not save_data_to_db(f"previous_signal_{symbol}", signal_doc, "Önceki Sinyal"):
            return False
        
        return True
    except Exception as e:
        print(f"❌ Önceki sinyal güncellenirken hata: {e}")
        return False

def remove_position_from_db(symbol):
    def delete_position():
        mongo_collection.delete_one({"_id": f"position_{symbol}"})
        print(f"✅ {symbol} pozisyonu MongoDB'den kaldırıldı")
        return True
    
    return safe_mongodb_operation(delete_position, f"{symbol} pozisyonu kaldırma", False)

app = None
global_stats = {
    "total_signals": 0,
    "successful_signals": 0,
    "failed_signals": 0,
    "total_profit_loss": 0.0,
    "active_signals_count": 0,
    "tracked_coins_count": 0
}
global_active_signals = {}
global_waiting_signals = {} 
global_successful_signals = {}
global_failed_signals = {}
global_positions = {} 
global_stop_cooldown = {} 
global_allowed_users = set() 
global_admin_users = set() 
global_last_signal_scan_time = None

def is_authorized_chat(update):
    """Kullanıcının yetkili olduğu sohbet mi kontrol et"""
    chat = update.effective_chat
    if not chat or not update.effective_user:
        return False
    
    user_id = update.effective_user.id
    
    # Sadece özel mesajlar destekleniyor
    if chat.type == "private":
        return user_id == BOT_OWNER_ID or user_id in ALLOWED_USERS or user_id in ADMIN_USERS
    
    return False

def should_respond_to_message(update):
    """Mesaja yanıt verilmeli mi kontrol et (sadece özel mesajlar destekleniyor)"""
    chat = update.effective_chat
    if not chat or not update.effective_user:
        return False
    
    user_id = update.effective_user.id
    # Sadece özel mesajlar destekleniyor
    if chat.type == "private":
        return user_id == BOT_OWNER_ID or user_id in ALLOWED_USERS or user_id in ADMIN_USERS
    
    return False

def is_admin(user_id):
    """Kullanıcının admin olup olmadığını kontrol et"""
    return user_id == BOT_OWNER_ID or user_id in ADMIN_USERS

async def send_telegram_message(message, chat_id=None):
    """Telegram mesajı gönder"""
    try:
        if not chat_id:
            chat_id = TELEGRAM_CHAT_ID
        
        if not chat_id:
            print("❌ Telegram chat ID bulunamadı!")
            return False
        
        # Connection pool ayarlarını güncelle
        connector = aiohttp.TCPConnector(
            limit=100,  # Bağlantı limitini artır
            limit_per_host=30,  # Host başına limit
            ttl_dns_cache=300,  # DNS cache süresi
            use_dns_cache=True,
            keepalive_timeout=30,
            enable_cleanup_closed=True
        )
        
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        
        async with aiohttp.ClientSession(
            connector=connector, 
            timeout=timeout,
            headers={'User-Agent': 'Mozilla/5.0'}
        ) as session:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = {
                'chat_id': chat_id,
                'text': message,
                'parse_mode': 'Markdown',
                'disable_web_page_preview': True
            }
            
            async with session.post(url, json=data, ssl=False) as response:
                if response.status == 200:
                    return True
                else:
                    response_text = await response.text()
                    print(f"❌ Telegram API hatası: {response.status} - {response_text}")
                    return False
                    
    except asyncio.TimeoutError:
        print(f"❌ Telegram mesaj gönderme timeout: {chat_id}")
        return False
    except Exception as e:
        print(f"❌ Mesaj gönderme hatası (chat_id: {chat_id}): {e}")
        return False

async def send_signal_to_all_users(message):
    sent_chats = set() 
    for user_id in ALLOWED_USERS:
        if str(user_id) not in sent_chats:
            try:
                await send_telegram_message(message, user_id)
                print(f"✅ Kullanıcıya sinyal gönderildi: {user_id}")
                sent_chats.add(str(user_id))
            except Exception as e:
                print(f"❌ Kullanıcıya sinyal gönderilemedi ({user_id}): {e}")
    

    # Grup/kanal desteği kaldırıldı - sadece özel mesajlar destekleniyor

async def send_admin_message(message):
    try:
        await send_telegram_message(message, BOT_OWNER_ID)
    except Exception as e:
        print(f"❌ Bot sahibine stop mesajı gönderilemedi: {e}")
        
async def help_command(update, context):
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS and user_id not in ADMIN_USERS:
        return 
    
    if user_id == BOT_OWNER_ID:
        help_text = """
👑 **Kripto Sinyal Botu Komutları (Bot Sahibi):**

📊 **Temel Komutlar:**
/help - Bu yardım mesajını göster
/stats - İstatistikleri göster
/active - Aktif sinyalleri göster
/test - Test sinyali gönder

👥 **Kullanıcı Yönetimi:**
/adduser <user_id> - Kullanıcı ekle
/removeuser <user_id> - Kullanıcı çıkar
/listusers - İzin verilen kullanıcıları listele

👑 **Admin Yönetimi:**
/adminekle <user_id> - Admin ekle
/adminsil <user_id> - Admin sil
/listadmins - Admin listesini göster

🧹 **Temizleme Komutları:**
/clearall - Tüm verileri temizle (pozisyonlar, önceki sinyaller, bekleyen kuyruklar, istatistikler)

🔧 **Özel Yetkiler:**
• Tüm komutlara erişim
• Admin ekleme/silme
• Veri temizleme
• Bot tam kontrolü
        """
    elif user_id in ADMIN_USERS:
        help_text = """
🛡️ **Kripto Sinyal Botu Komutları (Admin):**

📊 **Temel Komutlar:**
/help - Bu yardım mesajını göster
/stats - İstatistikleri göster
/active - Aktif sinyalleri göster
/test - Test sinyali gönder

👥 **Kullanıcı Yönetimi:**
/adduser <user_id> - Kullanıcı ekle
/removeuser <user_id> - Kullanıcı çıkar
/listusers - İzin verilen kullanıcıları listele

👑 **Admin Yönetimi:**
/listadmins - Admin listesini göster

🔧 **Yetkiler:**
• Kullanıcı ekleme/silme
• Test sinyali gönderme
• İstatistik görüntüleme
• Admin listesi görüntüleme
        """
    else:
        help_text = """
📱 **Kripto Sinyal Botu Komutları (Kullanıcı):**

📊 **Temel Komutlar:**
/help - Bu yardım mesajını göster
/active - Aktif sinyalleri göster

🔧 **Yetkiler:**
• Aktif sinyalleri görüntüleme
• Sinyal mesajlarını alma
        """
    
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=help_text,
            parse_mode='Markdown'
        )
        # Grup mesajını sil (isteğe bağlı)
        if update.message.chat.type != 'private':
            await update.message.delete()
    except Exception as e:
        print(f"❌ Özel mesaj gönderilemedi ({user_id}): {e}")
        # Markdown hatası durumunda HTML formatında dene
        try:
            # HTML formatına çevir - Markdown'ı HTML'e dönüştür
            html_text = help_text
            # **text** -> <b>text</b> dönüşümü
            html_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', html_text)
            await context.bot.send_message(
                chat_id=user_id,
                text=html_text,
                parse_mode='HTML'
            )
        except Exception as e2:
            print(f"❌ HTML formatında da gönderilemedi ({user_id}): {e2}")
            # Son çare olarak düz metin olarak gönder
            await context.bot.send_message(
                chat_id=user_id,
                text=help_text,
                parse_mode=None
            )

async def test_command(update, context):
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return 
    
    test_message = """🟢 ALIŞ SİNYALİ 🟢

🔹 Kripto Çifti: BTCUSDT  
💵 Giriş Fiyatı: $45,000.00
⚡ Kaldıraç: 10x
📊 24h Hacim: $2.5B

🎯 **HEDEF FİYATLAR:**
• Hedef 1 (%1): $45,450.00
• Hedef 2 (%2): $45,900.00
• Hedef 3 (%3): $46,350.00

🛑 **STOP LOSS:**
• SL 1: $45,227.25
• SL 2: $45,670.50
• SL 3: $46,113.75

⚠️ **Bu bir test sinyalidir!** ⚠️"""
    
    await update.message.reply_text("🧪 Test sinyali gönderiliyor...")    
    await send_signal_to_all_users(test_message)
    await update.message.reply_text("✅ Test sinyali başarıyla gönderildi!")

async def stats_command(update, context):
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        return 
    
    stats = load_stats_from_db() or global_stats
    if not stats:
        stats_text = "📊 **Bot İstatistikleri:**\n\nHenüz istatistik verisi yok."
    else:
        closed_count = stats.get('successful_signals', 0) + stats.get('failed_signals', 0)
        success_rate = 0
        if closed_count > 0:
            success_rate = (stats.get('successful_signals', 0) / closed_count) * 100
        
        computed_total = (
            stats.get('successful_signals', 0)
            + stats.get('failed_signals', 0)
            + stats.get('active_signals_count', 0)
        )
        
        status_emoji = "🟢"
        status_text = "Aktif (Sinyal Arama Çalışıyor)"
        # Markdown formatını güvenli hale getir
        safe_status_text = status_text.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')
        stats_text = f"""📊 **Bot İstatistikleri:**

📈 **Genel Durum:**
• Toplam Sinyal: {computed_total}
• Başarılı: {stats.get('successful_signals', 0)}
• Başarısız: {stats.get('failed_signals', 0)}
• Aktif Sinyal: {stats.get('active_signals_count', 0)}
• Takip Edilen Coin: {stats.get('tracked_coins_count', 0)}

💰 **Kar/Zarar (100$ yatırım):**
• Toplam: ${stats.get('total_profit_loss', 0):.2f}
• Başarı Oranı: %{success_rate:.1f}

🕒 **Son Güncelleme:** {datetime.now().strftime('%H:%M:%S')}
{status_emoji} **Bot Durumu:** {safe_status_text}"""
    
    # Aktif sinyallerin detaylı bilgilerini ekle
    active_signals = load_active_signals_from_db() or global_active_signals
    if active_signals:
        detailed_signals_text = "\n\n📊 **Aktif Sinyal Detayları:**\n"
        for symbol, signal in active_signals.items():
            try:
                entry_price = float(str(signal.get('entry_price', 0)).replace('$', '').replace(',', ''))
                current_price = float(str(signal.get('current_price', 0)).replace('$', '').replace(',', ''))
                
                # Yüzde değişimi hesapla
                if entry_price > 0:
                    signal_type = signal.get('type', 'ALIŞ')
                    if signal_type == "SATIŞ" or signal_type == "SATIS":
                        # SATIŞ sinyali için: Fiyat düşerse kar, yükselirse zarar
                        percent_change = ((entry_price - current_price) / entry_price) * 100
                    else:
                        # ALIŞ sinyali için: Fiyat yükselirse kar, düşerse zarar
                        percent_change = ((current_price - entry_price) / entry_price) * 100
                    
                    change_emoji = "🟢" if percent_change >= 0 else "🔴"
                    change_text = f"{percent_change:+.2f}%"
                else:
                    change_emoji = "⚪"
                    change_text = "0.00%"
                
                # Max/Min değerleri al (eğer varsa)
                max_price = signal.get('max_price', 0)
                min_price = signal.get('min_price', 0)
                
                if max_price and min_price:
                    signal_type = signal.get('type', 'ALIŞ')
                    if signal_type == "SATIŞ" or signal_type == "SATIS":
                        # SATIŞ sinyali için max/min hesaplama
                        max_percent = ((entry_price - float(str(max_price).replace('$', '').replace(',', ''))) / entry_price) * 100
                        min_percent = ((entry_price - float(str(min_price).replace('$', '').replace(',', ''))) / entry_price) * 100
                    else:
                        # ALIŞ sinyali için max/min hesaplama
                        max_percent = ((float(str(max_price).replace('$', '').replace(',', '')) - entry_price) / entry_price) * 100
                        min_percent = ((float(str(min_price).replace('$', '').replace(',', '')) - entry_price) / entry_price) * 100
                    
                    max_min_text = f" MAX: ({max_percent:+.2f}%) / MİN: ({min_percent:+.2f}%)"
                else:
                    max_min_text = ""
                
                # Sinyal tipini belirle
                signal_type = signal.get('type', 'ALIŞ')
                type_emoji = "🟢" if signal_type == "ALIŞ" else "🔴"
                
                detailed_signals_text += f"{change_emoji} {symbol} ({signal_type}):\nGiriş: ${entry_price:.6f}\nGüncel: ${current_price:.6f}\nDurum: ({change_text}){max_min_text}\n\n"
                
            except Exception as e:
                print(f"❌ {symbol} sinyal detayı hesaplanamadı: {e}")
                detailed_signals_text += f"⚪ {symbol}: Hata oluştu\n"
        
        stats_text += detailed_signals_text
    
    try:
        await update.message.reply_text(stats_text, parse_mode='Markdown')
    except Exception as e:
        print(f"❌ Markdown formatında gönderilemedi: {e}")
        # HTML formatında dene
        try:
            # Markdown'ı HTML'e dönüştür
            html_stats_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', stats_text)
            await update.message.reply_text(html_stats_text, parse_mode='HTML')
        except Exception as e2:
            print(f"❌ HTML formatında da gönderilemedi: {e2}")
            # Son çare olarak düz metin olarak gönder
            await update.message.reply_text(stats_text, parse_mode=None)

async def active_command(update, context):
    """Aktif sinyaller komutu"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS and user_id not in ADMIN_USERS:
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    active_signals = load_active_signals_from_db() or global_active_signals
    if not active_signals:
        active_text = "📈 **Aktif Sinyaller:**\n\nHenüz aktif sinyal yok."
    else:
        active_text = "📈 **Aktif Sinyaller:**\n\n"
        for symbol, signal in active_signals.items():
            # Markdown formatını güvenli hale getir
            safe_symbol = str(symbol).replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')
            safe_type = str(signal['type']).replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')
            safe_entry = str(signal['entry_price']).replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')
            safe_target = str(signal['target_price']).replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')
            safe_stop = str(signal['stop_loss']).replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')
            safe_current = str(signal['current_price']).replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')
            safe_leverage = str(signal['leverage']).replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')
            safe_time = str(signal['signal_time']).replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')
            
            active_text += f"""🔹 **{safe_symbol}** ({safe_type})
• Giriş: {safe_entry}
• Hedef: {safe_target}
• Stop: {safe_stop}
• Şu anki: {safe_current}
• Kaldıraç: {safe_leverage}x
• Sinyal: {safe_time}

"""
    
    try:
        await update.message.reply_text(active_text, parse_mode='Markdown')
    except Exception as e:
        print(f"❌ Markdown formatında gönderilemedi: {e}")
        # HTML formatında dene
        try:
            # Markdown'ı HTML'e dönüştür
            html_active_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', active_text)
            await update.message.reply_text(html_active_text, parse_mode='HTML')
        except Exception as e2:
            print(f"❌ HTML formatında da gönderilemedi: {e2}")
            # Son çare olarak düz metin olarak gönder
            await update.message.reply_text(active_text, parse_mode=None)

async def adduser_command(update, context):
    """Kullanıcı ekleme komutu (sadece bot sahibi ve adminler)"""
    user_id, is_authorized = validate_user_command(update, require_admin=True)
    if not is_authorized:
        return
    
    is_valid, error_msg = validate_command_args(update, context, 1)
    if not is_valid:
        await send_command_response(update, error_msg)
        return
    
    is_valid, new_user_id = validate_user_id(context.args[0])
    if not is_valid:
        await send_command_response(update, new_user_id)
        return
    
    if new_user_id == BOT_OWNER_ID:
        await send_command_response(update, "❌ Bot sahibi zaten her zaman erişime sahiptir.")
        return
    
    if new_user_id in ALLOWED_USERS:
        await send_command_response(update, "❌ Bu kullanıcı zaten izin verilen kullanıcılar listesinde.")
        return
    
    if new_user_id in ADMIN_USERS:
        await send_command_response(update, "❌ Bu kullanıcı zaten admin listesinde.")
        return
    
    ALLOWED_USERS.add(new_user_id)
    save_allowed_users()  # MongoDB'ye kaydet
    await send_command_response(update, f"✅ Kullanıcı {new_user_id} başarıyla eklendi ve kalıcı olarak kaydedildi.")

async def removeuser_command(update, context):
    """Kullanıcı çıkarma komutu (sadece bot sahibi ve adminler)"""
    user_id, is_authorized = validate_user_command(update, require_admin=True)
    if not is_authorized:
        return
    
    is_valid, error_msg = validate_command_args(update, context, 1)
    if not is_valid:
        await send_command_response(update, error_msg)
        return
    
    is_valid, remove_user_id = validate_user_id(context.args[0])
    if not is_valid:
        await send_command_response(update, remove_user_id)
        return
    
    if remove_user_id in ALLOWED_USERS:
        ALLOWED_USERS.remove(remove_user_id)
        save_allowed_users()  # MongoDB'ye kaydet
        await send_command_response(update, f"✅ Kullanıcı {remove_user_id} başarıyla çıkarıldı ve kalıcı olarak kaydedildi.")
    else:
        await send_command_response(update, f"❌ Kullanıcı {remove_user_id} zaten izin verilen kullanıcılar listesinde yok.")

async def listusers_command(update, context):
    """İzin verilen kullanıcıları listeleme komutu (sadece bot sahibi ve adminler)"""
    user_id, is_authorized = validate_user_command(update, require_admin=True)
    if not is_authorized:
        return
    
    if not ALLOWED_USERS:
        users_text = "📋 **İzin Verilen Kullanıcılar:**\n\nHenüz izin verilen kullanıcı yok."
    else:
        users_list = "\n".join([f"• {user_id}" for user_id in ALLOWED_USERS])
        users_text = f"📋 **İzin Verilen Kullanıcılar:**\n\n{users_list}"
    
    await send_command_response(update, users_text)

async def adminekle_command(update, context):
    """Admin ekleme komutu (sadece bot sahibi)"""
    user_id, is_authorized = validate_user_command(update, require_owner=True)
    if not is_authorized:
        return
    
    is_valid, error_msg = validate_command_args(update, context, 1)
    if not is_valid:
        await send_command_response(update, error_msg)
        return
    
    is_valid, new_admin_id = validate_user_id(context.args[0])
    if not is_valid:
        await send_command_response(update, new_admin_id)
        return
    
    if new_admin_id == BOT_OWNER_ID:
        await send_command_response(update, "❌ Bot sahibi zaten admin yetkilerine sahiptir.")
        return
    
    if new_admin_id in ADMIN_USERS:
        await send_command_response(update, "❌ Bu kullanıcı zaten admin listesinde.")
        return
    
    ADMIN_USERS.add(new_admin_id)
    save_admin_users()  # MongoDB'ye kaydet
    await send_command_response(update, f"✅ Admin {new_admin_id} başarıyla eklendi ve kalıcı olarak kaydedildi.")

async def adminsil_command(update, context):
    """Admin silme komutu (sadece bot sahibi)"""
    user_id, is_authorized = validate_user_command(update, require_owner=True)
    if not is_authorized:
        return
    
    is_valid, error_msg = validate_command_args(update, context, 1)
    if not is_valid:
        await send_command_response(update, error_msg)
        return
    
    is_valid, remove_admin_id = validate_user_id(context.args[0])
    if not is_valid:
        await send_command_response(update, remove_admin_id)
        return
    
    if remove_admin_id in ADMIN_USERS:
        ADMIN_USERS.remove(remove_admin_id)
        save_admin_users()  # MongoDB'ye kaydet
        await send_command_response(update, f"✅ Admin {remove_admin_id} başarıyla silindi ve kalıcı olarak kaydedildi.")
    else:
        await send_command_response(update, f"❌ Admin {remove_admin_id} zaten admin listesinde yok.")

async def listadmins_command(update, context):
    """Admin listesini gösterme komutu (sadece bot sahibi ve adminler)"""
    user_id, is_authorized = validate_user_command(update, require_admin=True)
    if not is_authorized:
        return
    
    if not ADMIN_USERS:
        admins_text = f"👑 **Admin Kullanıcıları:**\n\nHenüz admin kullanıcı yok.\n\nBot Sahibi: {BOT_OWNER_ID}"
    else:
        admins_list = "\n".join([f"• {admin_id}" for admin_id in ADMIN_USERS])
        admins_text = f"👑 **Admin Kullanıcıları:**\n\n{admins_list}\n\nBot Sahibi: {BOT_OWNER_ID}"
    
    await send_command_response(update, admins_text)

async def handle_message(update, context):
    """Genel mesaj handler'ı"""
    user_id, is_authorized = validate_user_command(update, require_admin=False)
    if not is_authorized:
        return
    
    if user_id == BOT_OWNER_ID or user_id in ALLOWED_USERS or user_id in ADMIN_USERS:
        await send_command_response(update, "🤖 Bu bot sadece komutları destekler. /help yazarak mevcut komutları görebilirsiniz.")

async def error_handler(update, context):
    """Hata handler'ı"""
    error = context.error
    
    # CancelledError'ları görmezden gel (bot kapatılırken normal)
    if isinstance(error, asyncio.CancelledError):
        print("ℹ️ Bot kapatılırken task iptal edildi (normal durum)")
        return
    
    if "Conflict" in str(error) and "getUpdates" in str(error):
        print("⚠️ Conflict hatası tespit edildi. Bot yeniden başlatılıyor...")
        try:
            # Webhook'ları temizle
            await app.bot.delete_webhook(drop_pending_updates=True)
            await asyncio.sleep(5)
            await app.updater.stop()
            await asyncio.sleep(2)
            await app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query", "chat_member", "my_chat_member", "channel_post"])
            
        except Exception as e:
            print(f"❌ Bot yeniden başlatma hatası: {e}")
        return
    
    # Diğer hataları logla
    print(f"Bot hatası: {error}")
    
    if update and update.effective_chat and update.effective_user:
        if update.effective_chat.type == "private":
            user_id, is_authorized = validate_user_command(update, require_admin=False)
            if is_authorized and not isinstance(context.error, telegram.error.TimedOut):
                try:
                    await send_command_response(update, "❌ Bir hata oluştu. Lütfen daha sonra tekrar deneyin.")
                except Exception as e:
                    print(f"❌ Error handler'da mesaj gönderme hatası: {e}")

async def handle_all_messages(update, context):
    """Tüm mesajları dinler ve loglar"""
    try:
        chat = update.effective_chat
        if not chat:
            return
        
        # Sadece özel mesajlar destekleniyor
        if chat.type == "private" and update.effective_user:
            user_id = update.effective_user.id
            if user_id == BOT_OWNER_ID:
                print(f"🔍 Bot sahibi mesajı: {update.message.text if update.message else 'N/A'}")
    
    except Exception as e:
        print(f"🔍 handle_all_messages Hatası: {e}")
    return

# handle_chat_member_update fonksiyonu kaldırıldı - artık grup/kanal desteği yok

async def setup_bot():
    """Bot handler'larını kur"""
    global app
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.get_updates(offset=-1, limit=1)
    except Exception as e:
        print(f"⚠️ Webhook temizleme hatası: {e}")
        print("🔄 Polling moduna geçiliyor...")
    
    # Komut handler'ları
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("active", active_command))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("listusers", listusers_command))
    app.add_handler(CommandHandler("adminekle", adminekle_command))
    app.add_handler(CommandHandler("adminsil", adminsil_command))
    app.add_handler(CommandHandler("listadmins", listadmins_command))
    app.add_handler(CommandHandler("clearall", clear_all_command))
    app.add_handler(CommandHandler("migrate", migrate_command))

    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Grup/kanal handler'ları kaldırıldı - sadece özel mesajlar destekleniyor
    
    # Kanal handler'ı kaldırıldı - sadece özel mesajlar destekleniyor
    app.add_error_handler(error_handler)
    
    print("Bot handler'ları kuruldu!")

def format_price(price, ref_price=None):
    if ref_price is not None:
        s = str(ref_price)
        if 'e' in s or 'E' in s:
            s = f"{ref_price:.20f}".rstrip('0').rstrip('.')
        if '.' in s:
            dec = len(s.split('.')[-1])
            getcontext().prec = dec + 8
            d_price = Decimal(str(price)).quantize(Decimal('1.' + '0'*dec), rounding=ROUND_DOWN)
            result = format(d_price, f'.{dec}f').rstrip('0').rstrip('.') if dec > 0 else str(int(d_price))
            return result
        else:
            return str(int(round(price)))
    else:
        # ref_price yoksa, eski davranış
        if price >= 1:
            return f"{price:.4f}".rstrip('0').rstrip('.')
        elif price >= 0.01:
            return f"{price:.6f}".rstrip('0').rstrip('.')
        elif price >= 0.0001:
            return f"{price:.8f}".rstrip('0').rstrip('.')
        else:
            return f"{price:.10f}".rstrip('0').rstrip('.')

def format_volume(volume):
    """Hacmi bin, milyon, milyar formatında formatla"""
    if volume >= 1_000_000_000:
        return f"${volume/1_000_000_000:.1f}B"
    elif volume >= 1_000_000:
        return f"${volume/1_000_000:.1f}M"
    elif volume >= 1_000:
        return f"${volume/1_000:.1f}K"
    else:
        return f"${volume:,.0f}"

async def create_signal_message_new_55(symbol, price, all_timeframes_signals, volume):
    """Kripto özel 2/2 sinyal sistemi - belirlenen timeframe kombinasyonlarını kontrol et, tek TP ve tek SL"""
    price_str = format_price(price, price)

    # Sadece desteklenen kriptolar için sinyal üret
    if symbol not in CRYPTO_SETTINGS:
        print(f"❌ {symbol} için ayar bulunamadı, sinyal üretilmiyor")
        return None, None, None, None, None, None, None

    crypto_config = CRYPTO_SETTINGS[symbol]
    timeframes = crypto_config["timeframes"]
    tp_percent = crypto_config["tp_percent"]
    sl_percent = crypto_config["sl_percent"]
    leverage = crypto_config["leverage"]

    # 15 dakikalık mum onayı için varsayılan değerler
    candle_color = "BİLİNMİYOR"
    is_bullish = None

    signal_values = []

    for tf in timeframes:
        signal_values.append(all_timeframes_signals.get(tf, 0))

    buy_signals = sum(1 for s in signal_values if s == 1)
    sell_signals = sum(1 for s in signal_values if s == -1)

    if buy_signals != 2 and sell_signals != 2:
        return None, None, None, None, None, None, None

    # 15 dakikalık mum onayı kontrolü
    try:
        print(f"🔍 {symbol} - 15 dakikalık mum onayı kontrol ediliyor...")
        df_15m = await async_get_historical_data(symbol, '15m', 2)  # Son 2 mum
        if df_15m is None or len(df_15m) < 1:
            print(f"⚠️ {symbol} - 15 dakikalık mum verisi alınamadı, sinyal reddedildi")
            return None, None, None, None, None, None, None

        # En son mumun verilerini al
        last_candle = df_15m.iloc[-1]
        open_price = float(last_candle['open'])
        close_price = float(last_candle['close'])

        print(f"🔍 {symbol} - 15m Mum: Açılış=${open_price:.6f}, Kapanış=${close_price:.6f}")

        # Mum rengi belirle
        if close_price > open_price:
            candle_color = "YEŞİL"
            is_bullish = True
        elif close_price < open_price:
            candle_color = "KIRMIZI"
            is_bullish = False
        else:
            candle_color = "NÖTR"
            is_bullish = None

        print(f"🔍 {symbol} - 15 dakikalık mum rengi: {candle_color}")

    except Exception as e:
        print(f"❌ {symbol} - 15 dakikalık mum kontrolü hatası: {e}")
        return None, None, None, None, None, None, None

    if buy_signals == 2 and sell_signals == 0:
        # ALIŞ sinyali için YEŞİL mum kontrolü
        if not is_bullish:
            print(f"❌ {symbol} - ALIŞ sinyali için YEŞİL mum gerekli, mevcut: {candle_color}. Sinyal reddedildi.")
            return None, None, None, None, None, None, None

        print(f"✅ {symbol} - ALIŞ sinyali için YEŞİL mum onayı alındı")
        sinyal_tipi = "🟢 ALIŞ SİNYALİ 🟢"
        dominant_signal = "ALIŞ"

        # Tek hedef fiyat hesapla
        target_price = price * (1 + tp_percent / 100)

        # Tek stop loss hesapla (GİRİŞ FİYATININ altı - ALIŞ için)
        stop_loss = price * (1 - sl_percent / 100)

    elif sell_signals == 2 and buy_signals == 0:
        # SATIŞ sinyali için KIRMIZI mum kontrolü
        if is_bullish:
            print(f"❌ {symbol} - SATIŞ sinyali için KIRMIZI mum gerekli, mevcut: {candle_color}. Sinyal reddedildi.")
            return None, None, None, None, None, None, None

        print(f"✅ {symbol} - SATIŞ sinyali için KIRMIZI mum onayı alındı")
        sinyal_tipi = "🔴 SATIŞ SİNYALİ 🔴"
        dominant_signal = "SATIŞ"

        # Tek hedef fiyat hesapla
        target_price = price * (1 - tp_percent / 100)

        # Tek stop loss hesapla (GİRİŞ FİYATININ üstü - SATIŞ için)
        stop_loss = price * (1 + sl_percent / 100)
        
    else:
        print(f"❌ Beklenmeyen durum: ALIŞ={buy_signals}, SATIŞ={sell_signals}")
        return None, None, None, None, None, None, None
    
    # leverage artık crypto_config'den geliyor 
    
    print(f"🧮 {symbol} SİNYAL HESAPLAMA:")
    print(f"   Giriş: ${price:.6f}")
    print(f"   Timeframes: {timeframes}")
    print(f"   TP: %{tp_percent} | SL: %{sl_percent}")
    if dominant_signal == "ALIŞ":
        print(f"   Hedef: ${price:.6f} + %{tp_percent} = ${target_price:.6f} (yukarı)")
        print(f"   Stop: ${price:.6f} - %{sl_percent} = ${stop_loss:.6f} (aşağı)")
    else:  # SATIŞ
        print(f"   Hedef: ${price:.6f} - %{tp_percent} = ${target_price:.6f} (aşağı)")
        print(f"   Stop: ${price:.6f} + %{sl_percent} = ${stop_loss:.6f} (yukarı)")
    
    # 2/2 kuralı: Belirtilen timeframe'ler aynı yönde olmalı
    if max(buy_signals, sell_signals) == 2:
        print(f"{symbol} - {' + '.join(timeframes)} 2/2 sinyal")
    
    target_price_str = format_price(target_price, price)
    stop_loss_str = format_price(stop_loss, price)
    volume_formatted = format_volume(volume)
    

    
    # Tek TP ve SL formatla
    
    message = f"""
{sinyal_tipi}

🔹 Kripto Çifti: {symbol}
💵 Giriş Fiyatı: {price_str}
⚡ Kaldıraç: {leverage}x
📊 24h Hacim: {volume_formatted}

⏰ Timeframes: {' + '.join(timeframes)}
🔍 15m Mum Onayı: {candle_color} ✅

🎯 <b>HEDEF FİYAT:</b>
• Hedef (%{tp_percent}): {target_price_str}

🛑 <b>STOP LOSS:</b>
• SL (%{sl_percent}): {stop_loss_str}

"""

    return message, dominant_signal, target_price, stop_loss, stop_loss_str, leverage, None

async def async_get_historical_data(symbol, interval, lookback):
    """Binance Futures'den geçmiş verileri asenkron çek"""
    if not symbol.endswith('USDT'):
        symbol = symbol + 'USDT'
    
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={lookback}"
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
            async with session.get(url, ssl=False) as resp:
                if resp.status != 200:
                    raise Exception(f"Futures API hatası: {resp.status} - {await resp.text()}")
                klines = await resp.json()
                if not klines or len(klines) == 0:
                    raise Exception(f"{symbol} için futures veri yok")
    except Exception as e:
        raise Exception(f"Futures veri çekme hatası: {symbol} - {interval} - {str(e)}")
    
    df = pd.DataFrame(klines, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades', 'taker_buy_base',
        'taker_buy_quote', 'ignored'
    ])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['volume'] = df['volume'].astype(float)
    df['open'] = df['open'].astype(float)
    return df

def calculate_full_pine_signals(df, timeframe):
    is_higher_tf = timeframe in ['5m']  # Sadece 5m yüksek timeframe olarak kabul edilir
    is_weekly = False  # Artık haftalık timeframe kullanılmıyor
    is_daily = False   # Artık günlük timeframe kullanılmıyor
    is_4h = False      # Artık 4h timeframe kullanılmıyor
    is_2h = False      # Artık 2h timeframe kullanılmıyor
    is_1h = False      # Artık 1h timeframe kullanılmıyor
    is_30m = False     # Artık 30m timeframe kullanılmıyor
    is_15m = False     # Artık 15m timeframe kullanılmıyor

    if is_weekly:
        rsi_length = 28
        macd_fast = 18
        macd_slow = 36
        macd_signal = 12
        short_ma_period = 30
        long_ma_period = 150
        mfi_length = 25
        fib_lookback = 150
        atr_period = 7
        volume_multiplier = 0.15
        rsi_overbought = 60
        rsi_oversold = 40
    elif is_daily:
        rsi_length = 21
        macd_fast = 13
        macd_slow = 26
        macd_signal = 10
        short_ma_period = 20
        long_ma_period = 100
        mfi_length = 20
        fib_lookback = 100
        atr_period = 7
        volume_multiplier = 0.15
        rsi_overbought = 60
        rsi_oversold = 40
    elif is_4h:
        rsi_length = 18
        macd_fast = 11
        macd_slow = 22
        macd_signal = 8
        short_ma_period = 12
        long_ma_period = 60
        mfi_length = 16
        fib_lookback = 70
        atr_period = 7
        volume_multiplier = 0.15
        rsi_overbought = 60
        rsi_oversold = 40
    elif is_2h:
        rsi_length = 16
        macd_fast = 10
        macd_slow = 21
        macd_signal = 8
        short_ma_period = 10
        long_ma_period = 55
        mfi_length = 15
        fib_lookback = 60
        atr_period = 8
        volume_multiplier = 0.25
        rsi_overbought = 60
        rsi_oversold = 40
    elif is_1h:
        rsi_length = 15
        macd_fast = 10
        macd_slow = 20
        macd_signal = 9
        short_ma_period = 9
        long_ma_period = 50
        mfi_length = 14
        fib_lookback = 50
        atr_period = 9
        volume_multiplier = 0.35
        rsi_overbought = 60
        rsi_oversold = 40
    elif is_30m:
        rsi_length = 14
        macd_fast = 10
        macd_slow = 20
        macd_signal = 9
        short_ma_period = 9
        long_ma_period = 50
        mfi_length = 14
        fib_lookback = 50
        atr_period = 10
        volume_multiplier = 0.4
        rsi_overbought = 60
        rsi_oversold = 40
    elif is_15m:
        rsi_length = 14
        macd_fast = 10
        macd_slow = 20
        macd_signal = 9
        short_ma_period = 9
        long_ma_period = 50
        mfi_length = 14
        fib_lookback = 50
        atr_period = 10
        volume_multiplier = 0.4
        rsi_overbought = 60
        rsi_oversold = 40
    elif timeframe == '8h':
        rsi_length = 17
        macd_fast = 10
        macd_slow = 21
        macd_signal = 8
        short_ma_period = 11
        long_ma_period = 65
        mfi_length = 16
        fib_lookback = 80
        atr_period = 8
        volume_multiplier = 0.2
        rsi_overbought = 60
        rsi_oversold = 40
    else:
        rsi_length = 14
        macd_fast = 10
        macd_slow = 20
        macd_signal = 9
        short_ma_period = 9
        long_ma_period = 50
        mfi_length = 14
        fib_lookback = 50
        atr_period = 10
        volume_multiplier = 0.4
        rsi_overbought = 60
        rsi_oversold = 40

    # EMA 200 ve trend
    df['ema200'] = ta.trend.EMAIndicator(df['close'], window=200).ema_indicator()
    df['trend_bullish'] = df['close'] > df['ema200']
    df['trend_bearish'] = df['close'] < df['ema200']

    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=rsi_length).rsi()

    macd = ta.trend.MACD(df['close'], window_slow=macd_slow, window_fast=macd_fast, window_sign=macd_signal)
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()

    def supertrend_dynamic(df, atr_period, timeframe):
        hl2 = (df['high'] + df['low']) / 2
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=atr_period).average_true_range()
        atr_dynamic = atr.rolling(window=5).mean()  # SMA(ATR, 5)
        
        if is_weekly:
            multiplier = atr_dynamic / 2
        elif is_daily:
            multiplier = atr_dynamic / 1.2
        elif is_4h:
            multiplier = atr_dynamic / 1.3
        elif is_2h:
            multiplier = atr_dynamic / 1.4
        elif is_1h:
            multiplier = atr_dynamic / 1.45
        elif timeframe == '8h':
            multiplier = atr_dynamic / 1.35
        else:
            multiplier = atr_dynamic / 1.5
            
        upperband = hl2 + multiplier
        lowerband = hl2 - multiplier
        direction = [1]
        supertrend_values = [lowerband.iloc[0]]
        
        for i in range(1, len(df)):
            if df['close'].iloc[i] > upperband.iloc[i-1]:
                direction.append(1)
                supertrend_values.append(lowerband.iloc[i])
            elif df['close'].iloc[i] < lowerband.iloc[i-1]:
                direction.append(-1)
                supertrend_values.append(upperband.iloc[i])
            else:
                direction.append(direction[-1])
                if direction[-1] == 1:
                    supertrend_values.append(lowerband.iloc[i])
                else:
                    supertrend_values.append(upperband.iloc[i])

        return pd.Series(direction, index=df.index), pd.Series(supertrend_values, index=df.index)

    df['supertrend_dir'], df['supertrend'] = supertrend_dynamic(df, atr_period, timeframe)
    df['short_ma'] = ta.trend.EMAIndicator(df['close'], window=short_ma_period).ema_indicator()
    df['long_ma'] = ta.trend.EMAIndicator(df['close'], window=long_ma_period).ema_indicator()
    df['ma_bullish'] = df['short_ma'] > df['long_ma']
    df['ma_bearish'] = df['short_ma'] < df['long_ma']

    volume_ma_period = 20
    df['volume_ma'] = df['volume'].rolling(window=volume_ma_period).mean()
    df['enough_volume'] = df['volume'] > df['volume_ma'] * volume_multiplier

    typical_price = (df['high'] + df['low'] + df['close']) / 3
    money_flow = typical_price * df['volume']
    
    positive_flow = []
    negative_flow = []
    
    for i in range(len(df)):
        if i == 0:
            positive_flow.append(0)
            negative_flow.append(0)
        else:
            if typical_price.iloc[i] > typical_price.iloc[i-1]:
                positive_flow.append(money_flow.iloc[i])
                negative_flow.append(0)
            elif typical_price.iloc[i] < typical_price.iloc[i-1]:
                positive_flow.append(0)
                negative_flow.append(money_flow.iloc[i])
            else:
                positive_flow.append(0)
                negative_flow.append(0)
    
    positive_flow_sum = pd.Series(positive_flow).rolling(window=mfi_length).sum()
    negative_flow_sum = pd.Series(negative_flow).rolling(window=mfi_length).sum()
    
    money_ratio = positive_flow_sum / (negative_flow_sum + 1e-10) 
    df['mfi'] = 100 - (100 / (1 + money_ratio))
    df['mfi_bullish'] = df['mfi'] < 65
    df['mfi_bearish'] = df['mfi'] > 35

    highest_high = df['high'].rolling(window=fib_lookback).max()
    lowest_low = df['low'].rolling(window=fib_lookback).min()
    fib_level1 = highest_high * 0.618
    fib_level2 = lowest_low * 1.382
    df['fib_in_range'] = (df['close'] > fib_level1) & (df['close'] < fib_level2)

    crossover = lambda s1, s2, shift=1: (s1.shift(shift) < s2.shift(shift)) & (s1 > s2)
    crossunder = lambda s1, s2, shift=1: (s1.shift(shift) > s2.shift(shift)) & (s1 < s2)

    # Sinyaller
    buy_signal = (
        crossover(df['macd'], df['macd_signal']) |
        (
            (df['rsi'] < rsi_oversold) &
            (df['supertrend_dir'] == 1) &
            df['ma_bullish'] &
            df['enough_volume'] &
            df['mfi_bullish'] &
            df['trend_bullish']
        )
    ) & df['fib_in_range']

    sell_signal = (
        crossunder(df['macd'], df['macd_signal']) |
        (
            (df['rsi'] > rsi_overbought) &
            (df['supertrend_dir'] == -1) &
            df['ma_bearish'] &
            df['enough_volume'] &
            df['mfi_bearish'] &
            df['trend_bearish']
        )
    ) & df['fib_in_range']

    df['signal'] = 0
    df.loc[buy_signal, 'signal'] = 1
    df.loc[sell_signal, 'signal'] = -1

    for i in range(len(df)):
        if df['signal'].iloc[i] == 0:
            if i > 0:
                df.at[df.index[i], 'signal'] = df['signal'].iloc[i-1]
            else:
                if df['macd'].iloc[i] > df['macd_signal'].iloc[i]:
                    df.at[df.index[i], 'signal'] = 1
                else:
                    df.at[df.index[i], 'signal'] = -1
    return df

async def get_active_high_volume_usdt_pairs(top_n=20, stop_cooldown=None):
    """
    Sadece CRYPTO_SETTINGS'deki 4 kripto için sinyal üretir
    stop_cooldown parametresi verilirse, cooldown'daki sembolleri filtreler
    """
    # Sadece belirlenen 4 kriptoyu kullan
    target_symbols = list(CRYPTO_SETTINGS.keys())  # ['SOLUSDT', 'AVAXUSDT', 'ETHUSDT', 'ADAUSDT']

    uygun_pairs = []
    for symbol in target_symbols:
        # COOLDOWN KONTROLÜ: Eğer stop_cooldown verilmişse, cooldown'daki sembolleri filtrele
        if stop_cooldown and check_cooldown(symbol, stop_cooldown, 2):  # COOLDOWN_HOURS
            print(f"⏰ {symbol} → Cooldown'da olduğu için sinyal arama listesine eklenmedi")
            continue

        try:
            # 1m timeframe verisi al (minimum veri kontrolü için)
            df_1m = await async_get_historical_data(symbol, '1m', 30)
            if len(df_1m) < 30:
                print(f"⚠️ {symbol} için yeterli veri bulunamadı, atlanıyor")
                continue
            uygun_pairs.append(symbol)
            print(f"✅ {symbol} → Sinyal kontrolü için uygun")
        except Exception as e:
            print(f"❌ {symbol} veri kontrolü hatası: {e}")
            continue

    print(f"📊 Kripto özel ayarlar için {len(uygun_pairs)}/{len(target_symbols)} kripto uygun")

    # Sembolleri her zaman yazdır (ilk çalıştırma kontrolü kaldırıldı)
    if uygun_pairs:
        print("📋 İşlenecek semboller:")
        group_str = ", ".join(uygun_pairs)
        print(f"   {group_str}")

    return uygun_pairs

async def check_signal_potential(symbol, positions, stop_cooldown, timeframes, tf_names, previous_signals):
    if symbol in positions:
        print(f"⏸️ {symbol} → Zaten aktif pozisyon var, yeni sinyal aranmıyor")
        return None
    
    # Stop cooldown kontrolü (2 saat)
    if check_cooldown(symbol, stop_cooldown, 2):
        # check_cooldown fonksiyonu zaten detaylı mesaj yazdırıyor
        return None

    try:
        # 1m timeframe verisi al (minimum veri kontrolü için)
        df_1m = await async_get_historical_data(symbol, '1m', 30)
        if df_1m is None or df_1m.empty:
            return None

        # Kripto özel timeframe'ler ile sinyal hesapla
        if symbol not in CRYPTO_SETTINGS:
            print(f"❌ {symbol} → CRYPTO_SETTINGS'de bulunamadı, sinyal üretilmiyor")
            return None
        
        crypto_config = CRYPTO_SETTINGS[symbol]
        symbol_timeframes = crypto_config["timeframes"]
        
        current_signals = await calculate_signals_for_symbol(symbol, {tf: tf for tf in symbol_timeframes}, symbol_timeframes)
        if current_signals is None:
            return None
        
        buy_count, sell_count = calculate_signal_counts(current_signals, symbol_timeframes)
        
        print(f"🔍 {symbol} → {' + '.join(symbol_timeframes)} sinyal analizi: ALIŞ={buy_count}, SATIŞ={sell_count}")
        
        if not check_2_2_rule(buy_count, sell_count):
            if buy_count > 0 or sell_count > 0:
                print(f"❌ {symbol} → {' + '.join(symbol_timeframes)} 2/2 kuralı sağlanmadı: ALIŞ={buy_count}, SATIŞ={sell_count} (2/2 olmalı!)")
                print(f"   Detay: {current_signals}")
            previous_signals[symbol] = current_signals.copy()
            return None
        
        print(f"✅ {symbol} → {' + '.join(symbol_timeframes)} 2/2 kuralı sağlandı! ALIŞ={buy_count}, SATIŞ={sell_count}")
        print(f"   Detay: {current_signals}")
        
        # Sinyal türünü belirle (2/2 kuralında sadece tek tip sinyal olmalı)
        if buy_count == 2 and sell_count == 0:
            sinyal_tipi = 'ALIŞ'
            dominant_signal = "ALIŞ"
        elif sell_count == 2 and buy_count == 0:
            sinyal_tipi = 'SATIŞ'
            dominant_signal = "SATIŞ"
        else:
            # Bu duruma asla gelmemeli çünkü 2/2 kuralı zaten kontrol edildi
            print(f"❌ {symbol} → Beklenmeyen durum: ALIŞ={buy_count}, SATIŞ={sell_count}")
            return None
        
        # Fiyat ve hacim bilgilerini al
        try:
            ticker_data = client.futures_ticker(symbol=symbol)
            
            # API bazen liste döndürüyor, bazen dict
            if isinstance(ticker_data, list):
                if len(ticker_data) == 0:
                    print(f"❌ {symbol} → Ticker verisi boş liste, sinyal iptal edildi")
                    return None
                ticker = ticker_data[0]  # İlk elementi al
            else:
                ticker = ticker_data
            
            if not ticker or not isinstance(ticker, dict):
                print(f"❌ {symbol} → Ticker verisi eksik veya hatalı format, sinyal iptal edildi")
                print(f"   Ticker: {ticker}")
                return None  # Sinyal iptal edildi
            
            if 'lastPrice' in ticker:
                price = float(ticker['lastPrice'])
            elif 'price' in ticker:
                price = float(ticker['price'])
            else:
                print(f"❌ {symbol} → Fiyat alanı bulunamadı (lastPrice/price), sinyal iptal edildi")
                print(f"   Ticker: {ticker}")
                return None
            
            volume_usd = float(ticker.get('quoteVolume', 0))
            
            if price <= 0 or volume_usd <= 0:
                print(f"❌ {symbol} → Fiyat ({price}) veya hacim ({volume_usd}) geçersiz, sinyal iptal edildi")
                return None  # Sinyal iptal edildi
                
        except Exception as e:
            print(f"❌ {symbol} → Fiyat/hacim bilgisi alınamadı: {e}, sinyal iptal edildi")
            return None  # Sinyal iptal edildi
        
        return {
            'symbol': symbol,
            'signals': current_signals,
            'price': price,
            'volume_usd': volume_usd,
            'signal_type': sinyal_tipi,
            'dominant_signal': dominant_signal,
            'buy_count': buy_count,
            'sell_count': sell_count
        }
        
    except Exception as e:
        print(f"❌ {symbol} sinyal potansiyeli kontrol hatası: {e}")
        return None

async def process_selected_signal(signal_data, positions, active_signals, stats):
    """Seçilen sinyali işler ve gönderir."""
    symbol = signal_data['symbol']
    current_signals = signal_data['signals']
    price = signal_data['price']
    volume_usd = signal_data['volume_usd']
    sinyal_tipi = signal_data['signal_type']
    
    # Aktif pozisyon kontrolü - eğer zaten aktif pozisyon varsa yeni sinyal gönderme
    if symbol in positions:
        print(f"⏸️ {symbol} → Zaten aktif pozisyon var, yeni sinyal gönderilmiyor")
        return
    
    try:
        # Mesaj oluştur ve gönder
        message, dominant_signal, target_price, stop_loss, stop_loss_str, leverage, _ = await create_signal_message_new_55(symbol, price, current_signals, volume_usd)
        
        if message:
            try:
                entry_price_float = float(price) if price is not None else 0.0
                target_price_float = float(target_price) if target_price is not None else 0.0
                stop_loss_float = float(stop_loss) if stop_loss is not None else 0.0
                leverage_int = int(leverage) if leverage is not None else 10
                
                # Geçerlilik kontrolü
                if entry_price_float <= 0 or target_price_float <= 0 or stop_loss_float <= 0:
                    print(f"⚠️ {symbol} - Geçersiz pozisyon verileri, pozisyon oluşturulmuyor")
                    print(f"   Giriş: {entry_price_float}, Hedef: {target_price_float}, Stop: {stop_loss_float}")
                    return
                
            except (ValueError, TypeError) as e:
                print(f"❌ {symbol} - Fiyat verisi dönüşüm hatası: {e}")
                print(f"   Raw values: price={price}, target={target_price}, stop={stop_loss}")
                return
            
            # Pozisyonu kaydet - DOMINANT_SIGNAL KULLAN VE TÜM DEĞERLER FLOAT OLARAK
            position = {
                "type": str(dominant_signal),  # dominant_signal kullan, sinyal_tipi değil!
                "target": target_price_float,  # Float olarak kaydet
                "stop": stop_loss_float,       # Float olarak kaydet
                "open_price": entry_price_float,  # Float olarak kaydet
                "stop_str": str(stop_loss_str),
                "signals": current_signals,
                "leverage": leverage_int,      # Int olarak kaydet
                "entry_time": str(datetime.now()),
                "entry_timestamp": datetime.now(),
            }
            
            # Pozisyonu dictionary'ye ekle
            positions[symbol] = position
            
            # Pozisyonu MongoDB'ye kaydet
            save_positions_to_db({symbol: position})
            
            # Aktif sinyale ekle - Artık save_positions_to_db tarafından yapılıyor
            # active_signals[symbol] = {...}  # Bu kısım kaldırıldı
            
            # save_active_signals_to_db(active_signals)  # Artık save_positions_to_db tarafından yapılıyor
            
            # İstatistikleri güncelle
            stats["total_signals"] += 1
            stats["active_signals_count"] = len(positions)  # positions kullan
            
            save_stats_to_db(stats)
            
            await send_signal_to_all_users(message)
            
            leverage_text = "10x" 
            print(f"✅ {symbol} {sinyal_tipi} sinyali gönderildi! Kaldıraç: {leverage_text}")
            
    except Exception as e:
        print(f"❌ {symbol} sinyal gönderme hatası: {e}")

async def check_existing_positions_and_cooldowns(positions, active_signals, stats, stop_cooldown):
    """Bot başlangıcında mevcut pozisyonları ve cooldown'ları kontrol eder"""
    print(f"🔍 [{datetime.now()}] Mevcut pozisyonlar ve cooldown'lar kontrol ediliyor... ({len(positions)} pozisyon)")
    for symbol in positions.keys():
        print(f"   📊 Kontrol edilecek pozisyon: {symbol}")

    # MongoDB'den mevcut pozisyonları yükle
    mongo_positions = load_positions_from_db()
    
    # 1. Aktif pozisyonları kontrol et
    for symbol in list(mongo_positions.keys()):
        try:
            # Pozisyon verilerinin geçerliliğini kontrol et
            position = mongo_positions[symbol]
            if not position or not isinstance(position, dict):
                print(f"⚠️ {symbol} - Geçersiz pozisyon verisi formatı, pozisyon temizleniyor")
                # MongoDB'den sil ama dictionary'den silme
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                continue
            
            # Veriyi hem yeni (data anahtarı) hem de eski yapıdan (doğrudan doküman) almaya çalış
            position_data = position.get('data', position)
            
            # Kritik alanların varlığını kontrol et
            required_fields = ['open_price', 'target', 'stop', 'type']
            missing_fields = [field for field in required_fields if field not in position_data]
            
            if missing_fields:
                print(f"⚠️ {symbol} - Eksik alanlar: {missing_fields}, pozisyon temizleniyor")
                # MongoDB'den sil ama dictionary'den silme
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                continue
            
            # Fiyat değerlerinin geçerliliğini kontrol et
            try:
                entry_price = float(position_data["open_price"])
                target_price = float(position_data["target"])
                stop_loss = float(position_data["stop"])
                signal_type = position_data["type"]
                
                if entry_price <= 0 or target_price <= 0 or stop_loss <= 0:
                    print(f"⚠️ {symbol} - Geçersiz pozisyon verileri, pozisyon temizleniyor")
                    print(f"   Giriş: {entry_price}, Hedef: {target_price}, Stop: {stop_loss}")
                    # MongoDB'den sil ama dictionary'den silme
                    mongo_collection.delete_one({"_id": f"position_{symbol}"})
                    mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                    continue
                    
            except (ValueError, TypeError) as e:
                print(f"⚠️ {symbol} - Fiyat dönüşüm hatası: {e}, pozisyon temizleniyor")
                # MongoDB'den sil ama dictionary'den silme
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                continue
            
            # Güncel fiyat bilgisini al
            df1m = await async_get_historical_data(symbol, '1m', 1)
            if df1m is None or df1m.empty:
                continue
            
            close_price = float(df1m['close'].iloc[-1])
            
            if signal_type == "ALIŞ" or signal_type == "ALIS":
                # ALIŞ pozisyonu için tek TP/SL kontrolü
                min_diff = 0.001  # %0.1 minimum fark
                
                # Kripto özel ayarlarından TP/SL değerlerini al
                crypto_config = CRYPTO_SETTINGS.get(symbol, CRYPTO_SETTINGS["SOLUSDT"])  # Varsayılan SOLUSDT
                tp_percent = crypto_config["tp_percent"]
                sl_percent = crypto_config["sl_percent"]
                leverage = crypto_config["leverage"]
                
                # Hedef fiyat kontrolü
                target_price = entry_price * (1 + tp_percent / 100)
                print(f"🔍 {symbol} TP DEBUG: Giriş: ${entry_price:.6f}, Hedef: ${target_price:.6f} (%{tp_percent}), Güncel: ${close_price:.6f}")
                
                if close_price >= target_price and (close_price - target_price) >= (target_price * min_diff):
                    print(f"🎯 {symbol} TP (%{tp_percent}) GERÇEKLEŞTİ!")
                    
                    profit_percentage = ((target_price - entry_price) / entry_price) * 100
                    profit_usd = (100 * (profit_percentage / 100)) * leverage
                    
                    target_message = f"""🎯 TP GERÇEKLEŞTİ! 🎯

🔹 Kripto Çifti: {symbol}
💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})
📈 Giriş: ${entry_price:.6f}
💵 Çıkış: ${target_price:.6f}
🎯 Hedef Seviyesi: TP (%{tp_percent})
🏁 Hedef gerçekleşti! Pozisyon kapatılıyor..."""
                    
                    await send_signal_to_all_users(target_message)
                    
                    # Başarılı sinyal olarak işaretle
                    stats["successful_signals"] += 1
                    print(f"🎯 {symbol} TP başarılı sinyal olarak işaretlendi")
                    
                    await process_position_close(symbol, "take_profit", target_price, positions, active_signals, stats, stop_cooldown, "TP")
                    continue
                
                # Stop loss kontrolü
                stop_loss = entry_price * (1 - sl_percent / 100)
                if close_price <= stop_loss and (stop_loss - close_price) >= (stop_loss * min_diff):
                    print(f"🛑 {symbol} SL (%{sl_percent}) GERÇEKLEŞTİ!")
                    
                    loss_percentage = ((entry_price - stop_loss) / entry_price) * 100
                    loss_usd = (100 * (loss_percentage / 100)) * leverage
                    
                    stop_message = f"""🛑 SL GERÇEKLEŞTİ! 🛑

🔹 Kripto Çifti: {symbol}
💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})
📈 Giriş: ${entry_price:.6f}
💵 Çıkış: ${stop_loss:.6f}
🛑 Stop Seviyesi: SL (%{sl_percent})
🏁 Stop tetiklendi! Pozisyon kapatılıyor..."""
                    
                    await send_signal_to_all_users(stop_message)
                    
                    # Başarısız sinyal olarak işaretle
                    stats["failed_signals"] += 1
                    print(f"🛑 {symbol} SL başarısız sinyal olarak işaretlendi")
                    
                    await process_position_close(symbol, "stop_loss", stop_loss, positions, active_signals, stats, stop_cooldown, "SL")
                    continue

                

                    
            # SATIŞ sinyali için hedef/stop kontrolü
            elif signal_type == "SATIŞ" or signal_type == "SATIS":
                # SATIŞ pozisyonu için tek TP/SL kontrolü
                min_diff = 0.001  # %0.1 minimum fark
                
                # Kripto özel ayarlarından TP/SL değerlerini al
                crypto_config = CRYPTO_SETTINGS.get(symbol, CRYPTO_SETTINGS["SOLUSDT"])  # Varsayılan SOLUSDT
                tp_percent = crypto_config["tp_percent"]
                sl_percent = crypto_config["sl_percent"]
                leverage = crypto_config["leverage"]
                
                # Hedef fiyat kontrolü (SATIŞ için fiyat düşmeli)
                target_price = entry_price * (1 - tp_percent / 100)
                print(f"🔍 {symbol} SATIŞ TP DEBUG: Giriş: ${entry_price:.6f}, Hedef: ${target_price:.6f} (%{tp_percent}), Güncel: ${close_price:.6f}")
                
                if close_price <= target_price and (target_price - close_price) >= (target_price * min_diff):
                    print(f"🎯 {symbol} SATIŞ TP (%{tp_percent}) GERÇEKLEŞTİ!")
                    
                    profit_percentage = ((entry_price - target_price) / entry_price) * 100
                    profit_usd = (100 * (profit_percentage / 100)) * leverage
                    
                    target_message = f"""🎯 SATIŞ TP GERÇEKLEŞTİ! 🎯

🔹 Kripto Çifti: {symbol}
💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})
📈 Giriş: ${entry_price:.6f}
💵 Çıkış: ${target_price:.6f}
🎯 Hedef Seviyesi: TP (%{tp_percent})
🏁 Hedef gerçekleşti! Pozisyon kapatılıyor..."""
                    
                    await send_signal_to_all_users(target_message)
                    
                    # Başarılı sinyal olarak işaretle
                    stats["successful_signals"] += 1
                    print(f"🎯 {symbol} SATIŞ TP başarılı sinyal olarak işaretlendi")
                    
                    await process_position_close(symbol, "take_profit", target_price, positions, active_signals, stats, stop_cooldown, "TP")
                    continue
                
                # Stop loss kontrolü (SATIŞ için fiyat yükselmeli)
                stop_loss = entry_price * (1 + sl_percent / 100)
                if close_price >= stop_loss and (close_price - stop_loss) >= (stop_loss * min_diff):
                    print(f"🛑 {symbol} SATIŞ SL (%{sl_percent}) GERÇEKLEŞTİ!")
                    
                    loss_percentage = ((stop_loss - entry_price) / entry_price) * 100
                    loss_usd = (100 * (loss_percentage / 100)) * leverage
                    
                    stop_message = f"""🛑 SATIŞ SL GERÇEKLEŞTİ! 🛑

🔹 Kripto Çifti: {symbol}
💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})
📈 Giriş: ${entry_price:.6f}
💵 Çıkış: ${stop_loss:.6f}
🛑 Stop Seviyesi: SL (%{sl_percent})
🏁 Stop tetiklendi! Pozisyon kapatılıyor..."""
                    
                    await send_signal_to_all_users(stop_message)
                    
                    # Başarısız sinyal olarak işaretle
                    stats["failed_signals"] += 1
                    print(f"🛑 {symbol} SATIŞ SL başarısız sinyal olarak işaretlendi")
                    
                    await process_position_close(symbol, "stop_loss", stop_loss, positions, active_signals, stats, stop_cooldown, "SL")
                    continue
                    
        except Exception as e:
            print(f"⚠️ {symbol} pozisyon kontrolü sırasında hata: {e}")
            continue
    
    expired_cooldowns = []
    for symbol, cooldown_time in list(stop_cooldown.items()):
        if isinstance(cooldown_time, str):
            cooldown_time = datetime.fromisoformat(cooldown_time)
        
        time_diff = (datetime.now() - cooldown_time).total_seconds() / 3600
        if time_diff >= 2:  # 2 saat geçmişse
            expired_cooldowns.append(symbol)
            print(f"✅ {symbol} cooldown süresi doldu, yeni sinyal aranabilir")
    
    # Süresi dolan cooldown'ları kaldır
    for symbol in expired_cooldowns:
        del stop_cooldown[symbol]
    if expired_cooldowns:
        save_stop_cooldown_to_db(stop_cooldown)
        print(f"🧹 {len(expired_cooldowns)} cooldown temizlendi")
    
    stats["active_signals_count"] = len(active_signals)
    save_stats_to_db(stats)
    
    print(f"✅ Bot başlangıcı kontrolü tamamlandı: {len(positions)} pozisyon, {len(active_signals)} aktif sinyal, {len(stop_cooldown)} cooldown")
    print("✅ Bot başlangıcı kontrolü tamamlandı")

async def signal_processing_loop():
    """Sinyal arama ve işleme döngüsü"""
    # Global değişkenleri tanımla
    global global_stats, global_active_signals, global_successful_signals, global_failed_signals, global_allowed_users, global_admin_users, global_positions, global_stop_cooldown

    positions = dict()  # {symbol: position_info}
    stop_cooldown = dict()  # {symbol: datetime}
    previous_signals = dict()  # {symbol: {tf: signal}} - İlk çalıştığında kaydedilen sinyaller
    active_signals = dict()  # {symbol: {...}} - Aktif sinyaller
    successful_signals = dict()  # {symbol: {...}} - Başarılı sinyaller (hedefe ulaşan)
    failed_signals = dict()  # {symbol: {...}} - Başarısız sinyaller (stop olan)
    tracked_coins = set()  # Takip edilen tüm coinlerin listesi
    
    stats = {
        "total_signals": 0,
        "successful_signals": 0,
        "failed_signals": 0,
        "total_profit_loss": 0.0,  # 100$ yatırım için
        "active_signals_count": 0,
        "tracked_coins_count": 0
    }
    
    # DB'de kayıtlı stats varsa yükle
    db_stats = load_stats_from_db()
    if db_stats:
        stats.update(db_stats)
    
    # Kripto özel timeframe'ler - Her kripto için farklı kombinasyon
    print("🚀 Bot başlatıldı! (Kripto özel timeframe kombinasyonları ile)")
    
    # İlk çalıştırma kontrolü
    is_first = is_first_run()
    if is_first:
        print("⏰ İlk çalıştırma: Kripto özel timeframe'ler ile mevcut sinyaller kaydediliyor, değişiklik bekleniyor...")
    else:
        print("🔄 Yeniden başlatma: Kripto özel timeframe'ler ile veritabanından pozisyonlar ve sinyaller yükleniyor...")
        # Pozisyonları yükle
        positions = load_positions_from_db()
        # Önceki sinyalleri yükle
        previous_signals = load_previous_signals_from_db()
        
        # Aktif sinyalleri DB'den yükle
        active_signals = load_active_signals_from_db()
        
        # Eğer DB'de aktif sinyal yoksa, pozisyonlardan oluştur
        if not active_signals:
            print("ℹ️ DB'de aktif sinyal bulunamadı, pozisyonlardan oluşturuluyor...")
            for symbol, pos in positions.items():
                active_signals[symbol] = {
                    "symbol": symbol,
                    "type": pos["type"],
                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                    "entry_price_float": pos["open_price"],
                    "target_price": format_price(pos["target"], pos["open_price"]),
                    "stop_loss": format_price(pos["stop"], pos["open_price"]),
                    "signals": pos["signals"],
                    "leverage": pos.get("leverage", 10),
                    "signal_time": pos.get("entry_time", datetime.now().strftime('%Y-%m-%d %H:%M')),
                    "current_price": format_price(pos["open_price"], pos["open_price"]),
                    "current_price_float": pos["open_price"],
                    "last_update": datetime.now().strftime('%Y-%m-%d %H:%M'),
                    "max_price": pos["open_price"],  # Başlangıçta max = giriş fiyatı
                    "min_price": pos["open_price"]   # Başlangıçta min = giriş fiyatı
                }
            
            # Yeni oluşturulan aktif sinyalleri DB'ye kaydet
            save_active_signals_to_db(active_signals)
        
        # İstatistikleri güncelle
        stats["active_signals_count"] = len(active_signals)
        save_stats_to_db(stats)
        
        # Bot başlangıcında mevcut durumları kontrol et
        await check_existing_positions_and_cooldowns(positions, active_signals, stats, stop_cooldown)
        
        # Global stop_cooldown değişkenini güncelle
        global_stop_cooldown = stop_cooldown.copy()
        
        # Bot başlangıcında eski sinyal cooldown'ları temizle
        print("🧹 Bot başlangıcında eski sinyal cooldown'ları temizleniyor...")
        await clear_cooldown_status()
    
    # Periyodik pozisyon kontrolü için sayaç
    position_check_counter = 0
    
    while True:
        try:
            if not ensure_mongodb_connection():
                print("⚠️ MongoDB bağlantısı kurulamadı, 30 saniye bekleniyor...")
                await asyncio.sleep(30)
                continue
            
            positions = load_positions_from_db()
            active_signals = load_active_signals_from_db()
            stats = load_stats_from_db()
            stop_cooldown = load_stop_cooldown_from_db()
            
            # Her 3 döngüde bir pozisyon kontrolü yap (yaklaşık 45 saniyede bir - TP mesajları için)
            position_check_counter += 1
            if position_check_counter >= 3:
                print(f"🔄 [{datetime.now()}] Periyodik pozisyon kontrolü yapılıyor... (Counter: {position_check_counter})")
                await check_existing_positions_and_cooldowns(positions, active_signals, stats, stop_cooldown)
                position_check_counter = 0
                print(f"✅ [{datetime.now()}] Periyodik pozisyon kontrolü tamamlandı")
                
                # Global stop_cooldown değişkenini güncelle
                global_stop_cooldown = stop_cooldown.copy()
            
            # Aktif sinyalleri positions ile senkronize et (her döngüde)
            for symbol in list(active_signals.keys()):
                if symbol not in positions:
                    # Sadece ilk kez mesaj yazdır
                    attr_name7 = f'_first_position_missing_{symbol}'
                    if not hasattr(signal_processing_loop, attr_name7):
                        print(f"⚠️ {symbol} → Positions'da yok, aktif sinyallerden kaldırılıyor")
                        setattr(signal_processing_loop, attr_name7, False)
                    del active_signals[symbol]
                    save_active_signals_to_db(active_signals)
                else:
                    # Positions'daki güncel verileri active_signals'a yansıt
                    position = positions[symbol]
                    if "entry_price_float" in active_signals[symbol]:
                        active_signals[symbol].update({
                            "target_price": format_price(position["target"], active_signals[symbol]["entry_price_float"]),
                            "stop_loss": format_price(position["stop"], active_signals[symbol]["entry_price_float"]),
                            "leverage": position.get("leverage", 10)
                        })
            
            # Stats'ı güncelle
            stats["active_signals_count"] = len(active_signals)
            save_stats_to_db(stats)
            
            # Her döngüde güncel durumu yazdır (senkronizasyon kontrolü için)
            print(f"📊 Güncel durum: {len(positions)} pozisyon, {len(active_signals)} aktif sinyal, {len(stop_cooldown)} cooldown")
            
            # Aktif pozisyonları ve cooldown'daki coinleri korumalı semboller listesine ekle
            protected_symbols = set(positions.keys()) | set(stop_cooldown.keys())
            
            # Sinyal arama için kullanılacak sembolleri filtrele
            # Cooldown'daki sembolleri sinyal arama listesine hiç ekleme
            print(f"🔍 Cooldown filtresi uygulanıyor... Mevcut cooldown sayısı: {len(stop_cooldown)}")
            new_symbols = await get_active_high_volume_usdt_pairs(20, stop_cooldown)  # İlk 20 sembol (cooldown filtrelenmiş)
            print(f"✅ Cooldown filtresi uygulandı. Filtrelenmiş sembol sayısı: {len(new_symbols)}")
            symbols = [s for s in new_symbols if s not in protected_symbols]
            
            if not symbols:
                # Sadece ilk kez mesaj yazdır
                if not hasattr(signal_processing_loop, '_first_all_protected'):
                    print("⚠️ Tüm coinler korumalı (aktif pozisyon veya cooldown)")
                    signal_processing_loop._first_all_protected = False
                await asyncio.sleep(60)
                continue
            
            # Cooldown durumunu kontrol et (sadece önceki döngüde çok fazla sinyal bulunduysa)
            cooldown_until = await check_cooldown_status()
            if cooldown_until and datetime.now() < cooldown_until:
                remaining_time = cooldown_until - datetime.now()
                remaining_minutes = int(remaining_time.total_seconds() / 60)
                print(f"⏳ Sinyal cooldown modunda, {remaining_minutes} dakika sonra tekrar sinyal aranacak.")
                print(f"   (Önceki döngüde çok fazla sinyal bulunduğu için)")
                await asyncio.sleep(60)  # 1 dakika bekle
                continue
            
            # Cooldown'daki kriptoların detaylarını göster
            if stop_cooldown:
                print(f"⏳ Cooldown'daki kriptolar ({len(stop_cooldown)} adet):")
                current_time = datetime.now()
                for symbol, cooldown_info in stop_cooldown.items():
                    # Debug: Cooldown bilgisini yazdır
                    print(f"🔍 DEBUG {symbol}: {type(cooldown_info)} = {cooldown_info}")
                    
                    if isinstance(cooldown_info, dict) and 'until' in cooldown_info:
                        cooldown_until = cooldown_info['until']
                        if isinstance(cooldown_until, str):
                            try:
                                cooldown_until = datetime.fromisoformat(cooldown_until.replace('Z', '+00:00'))
                            except:
                                cooldown_until = current_time
                        elif not isinstance(cooldown_until, datetime):
                            cooldown_until = current_time
                        
                        remaining_time = cooldown_until - current_time
                        if remaining_time.total_seconds() > 0:
                            remaining_minutes = int(remaining_time.total_seconds() / 60)
                            remaining_seconds = int(remaining_time.total_seconds() % 60)
                            print(f"   🔴 {symbol}: {remaining_minutes}dk {remaining_seconds}sn kaldı")
                        else:
                            print(f"   🟢 {symbol}: Cooldown süresi bitti")
                    else:
                        # Farklı format kontrolü - datetime objesi olabilir
                        if isinstance(cooldown_info, datetime):
                            # Correctly calculate the difference from the start time
                            time_diff_seconds = (current_time - cooldown_info).total_seconds()
                            cooldown_duration_seconds = 2 * 3600  # COOLDOWN_HOURS

                            if time_diff_seconds < cooldown_duration_seconds:
                                remaining_seconds = cooldown_duration_seconds - time_diff_seconds
                                remaining_minutes = int(remaining_seconds / 60)
                                remaining_seconds = int(remaining_seconds % 60)
                                print(f"   🔴 {symbol}: {remaining_minutes}dk {remaining_seconds}sn kaldı")
                            else:
                                print(f"   🟢 {symbol}: Cooldown süresi bitti")
                        else:
                            print(f"   ⚠️ {symbol}: Cooldown bilgisi eksik - Format: {type(cooldown_info)}")
                print()  # Boş satır ekle

            if not hasattr(signal_processing_loop, '_first_signal_search'):
                print("🚀 KRİPTO ÖZEL TIMEFRAME'LER İLE YENİ SİNYAL ARAMA BAŞLATILIYOR (aktif sinyal varken de devam eder)")
                signal_processing_loop._first_signal_search = True
            
            # Kripto özel timeframe'ler ile sinyal bulma mantığı - tüm uygun sinyalleri topla
            found_signals = {}  # Bulunan tüm sinyaller bu sözlükte toplanacak
            print(f"🔍 {len(symbols)} coin'de kripto özel timeframe'ler ile sinyal aranacak (aktif pozisyon: {len(positions)}, cooldown: {len(stop_cooldown)})")
            
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_crypto_count'):
                print(f"🔍 {len(symbols)} kripto özel timeframe'ler ile taranacak")
                signal_processing_loop._first_crypto_count = True
            
            # Aktif pozisyonları ve cooldown'daki coinleri koru
            protected_symbols = set()
            protected_symbols.update(positions.keys())  # Aktif pozisyonlar
            protected_symbols.update(stop_cooldown.keys())  # Cooldown'daki coinler
            
            # Yeni sembollere korunan sembolleri ekle (cooldown'daki semboller zaten filtrelenmiş)
            symbols = list(new_symbols)
            for protected_symbol in protected_symbols:
                if protected_symbol not in symbols:
                    symbols.append(protected_symbol)
            
            # Aktif pozisyonları ve cooldown'daki coinleri yeni sembol listesinden çıkar
            symbols = [s for s in symbols if s not in protected_symbols]
            
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_symbol_count'):
                print(f"📊 Toplam {len(symbols)} sembol kripto özel timeframe'ler ile kontrol edilecek (aktif pozisyonlar ve cooldown'daki coinler hariç)")
                signal_processing_loop._first_symbol_count = True

            print(f"📊 Toplam {len(symbols)} sembol kripto özel timeframe'ler ile kontrol edilecek...")
            processed_signals_in_loop = 0  # Bu döngüde işlenen sinyal sayacı
            
            # Cooldown süresi biten sinyalleri kontrol et ve aktif hale getir
            expired_cooldown_signals = await get_expired_cooldown_signals()
            if expired_cooldown_signals:
                print(f"🔄 Cooldown süresi biten {len(expired_cooldown_signals)} sinyal tekrar değerlendirilecek")
            
            # Tüm semboller için kripto özel timeframe'ler ile sinyal potansiyelini kontrol et ve topla
            for i, symbol in enumerate(symbols):
                # Her 20 sembolde bir ilerleme göster
                if (i + 1) % 20 == 0:  
                    print(f"⏳ {i+1}/{len(symbols)} sembol kripto özel timeframe'ler ile kontrol edildi...")

                # Halihazırda pozisyon varsa veya stop cooldown'daysa atla
                if symbol in positions:
                    continue
                if check_cooldown(symbol, stop_cooldown, 2):  # COOLDOWN_HOURS
                    continue
                
                # Sinyal cooldown kontrolü - süresi bitenler hariç
                if await check_signal_cooldown(symbol):
                    # Cooldown süresi biten sinyaller tekrar değerlendirilecek
                    if symbol in expired_cooldown_signals:
                        print(f"🔄 {symbol} cooldown süresi bitti, tekrar değerlendiriliyor")
                    else:
                        print(f"⏳ {symbol} sinyal cooldown'da, atlanıyor")
                        continue
                
                # Sinyal potansiyelini kontrol et
                signal_result = await check_signal_potential(
                    symbol, positions, stop_cooldown, None, None, previous_signals
                )
                
                # EĞER SİNYAL BULUNDUYSA, found_signals'a ekle
                if signal_result:
                    print(f"🔥 SİNYAL YAKALANDI: {symbol}!")
                    found_signals[symbol] = signal_result
            
            # Kripto özel timeframe'ler ile bulunan sinyalleri işle
            if not found_signals:
                print("🔍 Kripto özel timeframe'ler ile yeni sinyal bulunamadı.")
                # Sinyal bulunamadığında cooldown'ı temizle (normal çalışma modunda)
                await clear_cooldown_status()
                continue

            # Debug: Cooldown durumunu kontrol et
            cooldown_count = 0
            for symbol in symbols:
                if await check_signal_cooldown(symbol):
                    cooldown_count += 1
            print(f"📊 Cooldown durumu: {cooldown_count}/{len(symbols)} sembol cooldown'da")

            print(f"🎯 Kripto özel timeframe'ler ile toplam {len(found_signals)} sinyal bulundu!")
            
            # Hacim verilerini çekme ve sinyalleri filtreleme
            print("📊 Kripto özel timeframe'ler ile bulunan sinyallerin hacim verileri alınıyor...")
            volumes = await get_volumes_for_symbols(list(found_signals.keys()))

            # Hacim verisine göre sinyalleri sıralama
            sorted_signals = sorted(
                found_signals.items(),
                key=lambda item: volumes.get(item[0], 0),  # Hacmi bul, bulamazsa 0 varsay
                reverse=True  # En yüksek hacimden en düşüğe doğru sırala
            )

            # Çok fazla sinyal bulunduğunda sinyal cooldown kontrolü
            if len(sorted_signals) > 10:  # COOLDOWN_THRESHOLD
                print(f"🚨 {len(sorted_signals)} adet sinyal bulundu. Cooldown eşiğini (10) aştığı için:")
                print(f"   ✅ En yüksek hacimli 5 sinyal hemen verilecek")  # MAX_SIGNALS_PER_RUN
                print(f"   ⏳ Kalan {len(sorted_signals) - 5} sinyal 30 dakika cooldown'a girecek")  # COOLDOWN_MINUTES
                
                # En yüksek hacimli 5 sinyali hemen işle
                top_signals = sorted_signals[:5]  # MAX_SIGNALS_PER_RUN

                # Kalan sinyalleri cooldown'a ekle
                remaining_signals = [symbol for symbol, _ in sorted_signals[5:]]  # MAX_SIGNALS_PER_RUN
                if remaining_signals:
                    await set_signal_cooldown_to_db(remaining_signals, timedelta(minutes=30))  # COOLDOWN_MINUTES
                
            else:
                # Normal durum: 5 veya daha az sinyal varsa hepsini işle
                if len(sorted_signals) > 5:  # MAX_SIGNALS_PER_RUN
                    # 5'ten fazla ama cooldown eşiğinden az: En iyi 5'ini al
                    print(f"📊 {len(sorted_signals)} sinyal bulundu. En yüksek hacimli 5 sinyal işlenecek.")  # MAX_SIGNALS_PER_RUN
                    top_signals = sorted_signals[:5]  # MAX_SIGNALS_PER_RUN
                else:
                    # 5 veya daha az: Hepsi işlensin
                    top_signals = sorted_signals
                    print(f"📊 {len(sorted_signals)} sinyal bulundu. Tümü işlenecek.")

            # Kripto özel timeframe'ler ile seçilen sinyalleri işleme
            print(f"✅ Kripto özel timeframe'ler ile en yüksek hacimli {len(top_signals)} sinyal işleniyor...")
            for symbol, signal_result in top_signals:
                crypto_config = CRYPTO_SETTINGS.get(symbol, {})
                symbol_timeframes = crypto_config.get("timeframes", ["N/A"])
                print(f"🚀 {symbol} {' + '.join(symbol_timeframes)} kombinasyonu sinyali işleniyor (Hacim: ${volumes.get(symbol, 0):,.0f})")
                await process_selected_signal(signal_result, positions, active_signals, stats)
                processed_signals_in_loop += 1
            
            print(f"✅ Kripto özel timeframe'ler ile tarama döngüsü tamamlandı. Bu turda {processed_signals_in_loop} yeni sinyal işlendi.")

            if is_first:
                print(f"💾 İlk çalıştırma: {len(previous_signals)} sinyal kaydediliyor...")
                if len(previous_signals) > 0:
                    save_previous_signals_to_db(previous_signals)
                    print("✅ İlk çalıştırma sinyalleri kaydedildi!")
                else:
                    print("ℹ️ İlk çalıştırmada kayıt edilecek sinyal bulunamadı")
                is_first = False  # Artık ilk çalıştırma değil

            if not hasattr(signal_processing_loop, '_first_loop'):
                print("🚀 Kripto özel timeframe'ler ile yeni sinyal aramaya devam ediliyor...")
                signal_processing_loop._first_loop = False
        
            # Kripto özel timeframe'ler ile aktif sinyallerin fiyatlarını güncelle ve hedef/stop kontrolü yap
            if active_signals:
                # Sadece ilk kez mesaj yazdır
                if not hasattr(signal_processing_loop, '_first_active_check'):
                    print(f"🔍 KRİPTO ÖZEL TIMEFRAME'LER İLE AKTİF SİNYAL KONTROLÜ BAŞLATILIYOR... ({len(active_signals)} aktif sinyal)")
                    for symbol in list(active_signals.keys()):
                        crypto_config = CRYPTO_SETTINGS.get(symbol, {})
                        symbol_timeframes = crypto_config.get("timeframes", ["N/A"])
                        print(f"   📊 {symbol} ({' + '.join(symbol_timeframes)}): {active_signals[symbol].get('type', 'N/A')} - Giriş: ${active_signals[symbol].get('entry_price_float', 0):.6f}")
                    signal_processing_loop._first_active_check = False
                
                print(f"🔍 DEBUG: {len(active_signals)} aktif sinyal kontrol edilecek")
                for symbol in list(active_signals.keys()):
                    crypto_config = CRYPTO_SETTINGS.get(symbol, {})
                    symbol_timeframes = crypto_config.get("timeframes", ["N/A"])
                    print(f"   🔍 {symbol} ({' + '.join(symbol_timeframes)}): {active_signals[symbol].get('type', 'N/A')} - Giriş: ${active_signals[symbol].get('entry_price_float', 0):.6f}")
            else:
                # Sadece ilk kez mesaj yazdır
                if not hasattr(signal_processing_loop, '_first_no_active'):
                    print("ℹ️ Kripto özel timeframe'ler ile aktif sinyal yok, kontrol atlanıyor")
                    signal_processing_loop._first_no_active = False
                continue
            
            for symbol in list(active_signals.keys()):
                print(f"🔍 DEBUG: {symbol} pozisyon kontrolü yapılıyor...")
                if symbol not in positions:  # Pozisyon kapandıysa aktif sinyalden kaldır
                    print(f"⚠️ {symbol} → Positions'da yok, aktif sinyallerden kaldırılıyor")
                    del active_signals[symbol]
                    continue
                
                try:
                    # Sadece ilk kez mesaj yazdır
                    attr_name = f'_first_active_check_{symbol}'
                    if not hasattr(signal_processing_loop, attr_name):
                        print(f"🔍 {symbol} 15m+30m kombinasyonu aktif sinyal kontrolü başlatılıyor...")
                        setattr(signal_processing_loop, attr_name, False)
                    
                    # Güncel fiyat bilgisini al
                    df1m = await async_get_historical_data(symbol, '1m', 1)
                    if df1m is None or df1m.empty:
                        print(f"⚠️ {symbol} → 1m veri alınamadı")
                        continue
                    
                    # Güncel fiyat
                    last_price = float(df1m['close'].iloc[-1])
                    print(f"🔍 DEBUG: {symbol} → Giriş: ${active_signals[symbol]['entry_price_float']:.6f}, Güncel: ${last_price:.6f}")
                    active_signals[symbol]["current_price"] = format_price(last_price, active_signals[symbol]["entry_price_float"])
                    active_signals[symbol]["current_price_float"] = last_price
                    active_signals[symbol]["last_update"] = str(datetime.now())
                    
                    # Max/Min değerleri güncelle
                    if "max_price" not in active_signals[symbol] or last_price > active_signals[symbol]["max_price"]:
                        active_signals[symbol]["max_price"] = last_price
                    if "min_price" not in active_signals[symbol] or last_price < active_signals[symbol]["min_price"]:
                        active_signals[symbol]["min_price"] = last_price
                    
                    # Aktif sinyalleri veritabanına kaydet
                    save_active_signals_to_db(active_signals)
                    
                    # Hedef ve stop kontrolü
                    entry_price = active_signals[symbol]["entry_price_float"]
                    target_price = float(active_signals[symbol]["target_price"].replace('$', '').replace(',', ''))
                    stop_loss = float(active_signals[symbol]["stop_loss"].replace('$', '').replace(',', ''))
                    signal_type = active_signals[symbol]["type"]
                    
                    print(f"🔍 DEBUG: {symbol} → Sinyal tipi: {signal_type}, Hedef: ${target_price:.6f}, Stop: ${stop_loss:.6f}")
                    
                    # Sadece ilk kez mesaj yazdır
                    attr_name2 = f'_first_control_values_{symbol}'
                    if not hasattr(signal_processing_loop, attr_name2):
                        print(f"   📊 {symbol} 15m+30m kombinasyonu kontrol değerleri:")
                        print(f"      Giriş: ${entry_price:.6f}")
                        print(f"      Hedef: ${target_price:.6f}")
                        print(f"      Stop: ${stop_loss:.6f}")
                        print(f"      Güncel: ${last_price:.6f}")
                        print(f"      Güncel: ${last_price:.6f}")
                        print(f"      Sinyal: {signal_type}")
                        setattr(signal_processing_loop, attr_name2, False)
                    
                    # 15m+30m kombinasyonu ALIŞ sinyali için hedef/stop kontrolü
                    if signal_type == "ALIŞ" or signal_type == "ALIS":
                        print(f"🔍 DEBUG: {symbol} → ALIŞ sinyali kontrol ediliyor...")
                        # Sadece ilk kez mesaj yazdır
                        attr_name3 = f'_first_alish_check_{symbol}'
                        if not hasattr(signal_processing_loop, attr_name3):
                            print(f"   🔍 {symbol} 15m+30m kombinasyonu ALIŞ sinyali kontrol ediliyor...")
                            setattr(signal_processing_loop, attr_name3, False)
                        
                        # 15m+30m kombinasyonu hedef kontrolü: Güncel fiyat hedefi geçti mi? (ALIŞ: yukarı çıkması gerekir)
                        # GÜVENLİK KONTROLÜ: Fiyat gerçekten hedefi geçti mi?
                        # Minimum fark kontrolü: Fiyat hedefi en az 0.1% geçmeli (daha güvenli)
                        min_target_diff = target_price * 0.001  # %0.1 minimum fark
                        print(f"🔍 DEBUG: {symbol} → ALIŞ hedef kontrolü: Güncel: ${last_price:.6f}, Hedef: ${target_price:.6f}, Min fark: ${min_target_diff:.6f}")
                        if last_price >= target_price and (last_price - target_price) >= min_target_diff:
                            print(f"🎯 DEBUG: {symbol} → ALIŞ hedefi gerçekleşti! Güncel: ${last_price:.6f}, Hedef: ${target_price:.6f}")
                            # Kripto özel timeframe'ler ile HEDEF OLDU! 🎯
                            # Güvenli kâr hesaplaması
                            if entry_price > 0:
                                profit_percentage = ((target_price - entry_price) / entry_price) * 100
                                leverage = active_signals[symbol].get('leverage', 10)
                                profit_usd = (100 * (profit_percentage / 100)) * leverage
                            else:
                                profit_percentage = 0
                                profit_usd = 0
                            
                            print(f"🎯 HEDEF OLDU! {symbol} - Giriş: ${entry_price:.4f}, Hedef: ${target_price:.4f}, Çıkış: ${last_price:.4f}")
                            print(f"💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})")
                            
                            # Başarılı sinyali kaydet
                            successful_signals[symbol] = {
                                "symbol": symbol,
                                "type": signal_type,
                                "entry_price": entry_price,
                                "target_price": target_price,
                                "exit_price": last_price,
                                "profit_percentage": profit_percentage,
                                "profit_usd": profit_usd,
                                "entry_time": active_signals[symbol]["signal_time"],
                                "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                                "duration": "Hedef"
                            }
                            
                            # İstatistikleri güncelle
                            stats["successful_signals"] += 1
                            stats["total_profit_loss"] += profit_usd
                            
                            # Stop cooldown'a ekle
                            stop_cooldown[symbol] = datetime.now()
                            
                            # Cooldown'ı veritabanına kaydet
                            save_stop_cooldown_to_db(stop_cooldown)
                            
                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in positions:
                                del positions[symbol]
                            del active_signals[symbol]
                            
                            # Global değişkenleri hemen güncelle (hızlı kontrol için)
                            global_active_signals = active_signals.copy()
                            global_positions = positions.copy()
                            global_stats = stats.copy()
                            global_stop_cooldown = stop_cooldown.copy()
                            global_successful_signals = successful_signals.copy()
                            global_failed_signals = failed_signals.copy()
                            
                            # MESAJ GÖNDERİMİ KALDIRILDI - monitor_signals() fonksiyonu mesaj gönderecek
                            print(f"📢 Hedef mesajı monitor_signals() tarafından gönderilecek")
                            
                        # Kripto özel timeframe'ler stop kontrolü: Güncel fiyat stop'u geçti mi? (ALIŞ: aşağı düşmesi zarar)
                        # GÜVENLİK KONTROLÜ: Fiyat gerçekten stop'u geçti mi?
                        # Minimum fark kontrolü: Fiyat stop'u en az 0.1% geçmeli (daha güvenli)
                        min_stop_diff = stop_loss * 0.001  # %0.1 minimum fark
                        if last_price <= stop_loss and (stop_loss - last_price) >= min_stop_diff:
                            
                            # Kripto özel timeframe'ler ile STOP OLDU! 🛑
                            # Güvenli zarar hesaplaması
                            if entry_price > 0:
                                loss_percentage = ((entry_price - stop_loss) / entry_price) * 100
                                leverage = active_signals[symbol].get('leverage', 10)
                                loss_usd = (100 * (loss_percentage / 100)) * leverage
                            else:
                                loss_percentage = 0
                                loss_usd = 0
                            
                            print(f"🛑 STOP OLDU! {symbol} - Giriş: ${entry_price:.4f}, Stop: ${stop_loss:.4f}, Çıkış: ${last_price:.4f}")
                            print(f"💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})")
                            
                            # Başarısız sinyali kaydet
                            failed_signals[symbol] = {
                                "symbol": symbol,
                                "type": signal_type,
                                "entry_price": entry_price,
                                "stop_loss": stop_loss,
                                "exit_price": last_price,
                                "loss_percentage": loss_percentage,
                                "loss_usd": loss_usd,
                                "entry_time": active_signals[symbol]["signal_time"],
                                "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                                "duration": "Stop"
                            }
                            
                            # İstatistikleri güncelle
                            stats["failed_signals"] += 1
                            stats["total_profit_loss"] -= loss_usd
                            
                            # Stop cooldown'a ekle
                            stop_cooldown[symbol] = datetime.now()
                            
                            # Cooldown'ı veritabanına kaydet
                            save_stop_cooldown_to_db(stop_cooldown)
                            
                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in positions:
                                del positions[symbol]
                            del active_signals[symbol]
                            
                            # MESAJ GÖNDERİMİ KALDIRILDI - monitor_signals() fonksiyonu mesaj gönderecek
                            print(f"📢 Stop mesajı monitor_signals() tarafından gönderilecek")
                    
                    # 15m+30m kombinasyonu SATIŞ sinyali için hedef/stop kontrolü
                    elif signal_type == "SATIŞ" or signal_type == "SATIS":
                        print(f"🔍 DEBUG: {symbol} → SATIŞ sinyali kontrol ediliyor...")
                        # Sadece ilk kez mesaj yazdır
                        attr_name4 = f'_first_satish_check_{symbol}'
                        if not hasattr(signal_processing_loop, attr_name4):
                            print(f"🔍 {symbol} 15m+30m kombinasyonu SATIŞ sinyali kontrol ediliyor...")
                            setattr(signal_processing_loop, attr_name4, False)
                        # 15m+30m kombinasyonu hedef kontrolü: Güncel fiyat hedefi geçti mi? (SATIŞ: aşağı düşmesi gerekir)
                        # GÜVENLİK KONTROLÜ: Fiyat gerçekten hedefi geçti mi?
                        # Minimum fark kontrolü: Fiyat hedefi en az 0.1% geçmeli (daha güvenli)
                        min_target_diff = target_price * 0.001  # %0.1 minimum fark
                        print(f"🔍 DEBUG: {symbol} → SATIŞ hedef kontrolü: Güncel: ${last_price:.6f}, Hedef: ${target_price:.6f}, Min fark: ${min_target_diff:.6f}")
                        if last_price <= target_price and (target_price - last_price) >= min_target_diff:
                            print(f"🎯 DEBUG: {symbol} → SATIŞ hedefi gerçekleşti! Güncel: ${last_price:.6f}, Hedef: ${target_price:.6f}")
                            # Kripto özel timeframe'ler ile HEDEF OLDU! 🎯
                            # Güvenli kâr hesaplaması
                            if entry_price > 0:
                                profit_percentage = ((entry_price - target_price) / entry_price) * 100
                                # Kripto özel leverage ile hesapla
                                crypto_config = CRYPTO_SETTINGS.get(symbol, {})
                                leverage = crypto_config.get("leverage", 10)
                                profit_usd = 100 * (profit_percentage / 100) * leverage  # 100$ yatırım için
                            else:
                                profit_percentage = 0
                                profit_usd = 0
                            
                            print(f"🎯 HEDEF OLDU! {symbol} - Giriş: ${entry_price:.4f}, Hedef: ${target_price:.4f}, Çıkış: ${last_price:.4f}")
                            print(f"💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})")
                            
                            # Başarılı sinyali kaydet
                            successful_signals[symbol] = {
                                "symbol": symbol,
                                "type": signal_type,
                                "entry_price": entry_price,
                                "target_price": target_price,
                                "exit_price": last_price,
                                "profit_percentage": profit_percentage,
                                "profit_usd": profit_usd,
                                "entry_time": active_signals[symbol]["signal_time"],
                                "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                                "duration": "Hedef"
                            }
                            
                            # İstatistikleri güncelle
                            stats["successful_signals"] += 1
                            stats["total_profit_loss"] += profit_usd
                            
                            # Stop cooldown'a ekle
                            stop_cooldown[symbol] = datetime.now()
                            
                            # Cooldown'ı veritabanına kaydet
                            save_stop_cooldown_to_db(stop_cooldown)
                            
                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in positions:
                                del positions[symbol]
                            del active_signals[symbol]
                            
                            # Global değişkenleri hemen güncelle (hızlı kontrol için)
                            global_active_signals = active_signals.copy()
                            global_positions = positions.copy()
                            global_stats = stats.copy()
                            global_stop_cooldown = stop_cooldown.copy()
                            global_successful_signals = successful_signals.copy()
                            global_failed_signals = failed_signals.copy()

                            print(f"📢 Hedef mesajı monitor_signals() tarafından gönderilecek")
                            
                        # Kripto özel timeframe'ler stop kontrolü: Güncel fiyat stop'u geçti mi? (SATIŞ: yukarı çıkması zarar)
                        # GÜVENLİK KONTROLÜ: Fiyat gerçekten stop'u geçti mi?
                        # Minimum fark kontrolü: Fiyat stop'u en az 0.1% geçmeli (daha güvenli)
                        min_stop_diff = stop_loss * 0.001  # %0.1 minimum fark
                        if last_price >= stop_loss and (last_price - stop_loss) >= min_stop_diff:
                            
                            # Kripto özel timeframe'ler ile STOP OLDU! 🛑
                            # Güvenli zarar hesaplaması
                            if entry_price > 0:
                                loss_percentage = ((stop_loss - entry_price) / entry_price) * 100
                                # Kripto özel leverage ile hesapla
                                crypto_config = CRYPTO_SETTINGS.get(symbol, {})
                                leverage = crypto_config.get("leverage", 10)
                                loss_usd = 100 * (loss_percentage / 100) * leverage  # 100$ yatırım için
                            else:
                                loss_percentage = 0
                                loss_usd = 0
                            
                            print(f"🛑 STOP OLDU! {symbol} - Giriş: ${entry_price:.4f}, Stop: ${stop_loss:.4f}, Çıkış: ${last_price:.4f}")
                            print(f"💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})")
                            
                            # Başarısız sinyali kaydet
                            failed_signals[symbol] = {
                                "symbol": symbol,
                                "type": signal_type,
                                "entry_price": entry_price,
                                "stop_loss": stop_loss,
                                "exit_price": last_price,
                                "loss_percentage": loss_percentage,
                                "loss_usd": loss_usd,
                                "entry_time": active_signals[symbol]["signal_time"],
                                "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                                "duration": "Stop"
                            }
                            
                            # İstatistikleri güncelle
                            stats["failed_signals"] += 1
                            stats["total_profit_loss"] -= loss_usd
                            
                            # Stop cooldown'a ekle
                            stop_cooldown[symbol] = datetime.now()
                            
                            # Cooldown'ı veritabanına kaydet
                            save_stop_cooldown_to_db(stop_cooldown)
                            
                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in positions:
                                del positions[symbol]
                            del active_signals[symbol]
                            
                            # MESAJ GÖNDERİMİ KALDIRILDI - monitor_signals() fonksiyonu mesaj gönderecek
                            print(f"📢 Stop mesajı monitor_signals() tarafından gönderilecek")
                    
                except Exception as e:
                    print(f"Aktif sinyal güncelleme hatası: {symbol} - {str(e)}")
                    continue
            
            # Aktif sinyal kontrolü özeti
            if active_signals:
                print(f"✅ AKTİF SİNYAL KONTROLÜ TAMAMLANDI ({len(active_signals)} sinyal)")
                for symbol in list(active_signals.keys()):
                    if symbol in active_signals:
                        entry_price = active_signals[symbol].get("entry_price_float", 0)
                        
                        try:
                            ticker = client.futures_ticker(symbol=symbol)
                            current_price = float(ticker['lastPrice'])
                        except Exception as e:
                            current_price = active_signals[symbol].get("current_price_float", 0)
                            print(f"   ⚠️ {symbol}: İstatistik için gerçek zamanlı fiyat alınamadı, kayıtlı değer kullanılıyor: ${current_price:.6f} (Hata: {e})")
                        
                        if current_price > 0 and entry_price > 0:
                            # Pozisyon tipine göre kâr/zarar hesaplama
                            signal_type = active_signals[symbol].get('type', 'ALIŞ')
                            if signal_type == "ALIŞ" or signal_type == "ALIS":
                                change_percent = ((current_price - entry_price) / entry_price) * 100
                            else:  # SATIŞ
                                change_percent = ((entry_price - current_price) / entry_price) * 100
                            print(f"   📊 {symbol}: Giriş: ${entry_price:.6f} → Güncel: ${current_price:.6f} (%{change_percent:+.2f})")
            else:
                print("ℹ️ Aktif sinyal kalmadı")
            
            # Aktif sinyalleri dosyaya kaydet
            with open('active_signals.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "active_signals": active_signals,
                    "count": len(active_signals),
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
            
            # İstatistikleri güncelle
            stats["active_signals_count"] = len(active_signals)
            stats["tracked_coins_count"] = len(tracked_coins)
            
            # Global değişkenleri güncelle (bot komutları için)
            global_stats = stats.copy()
            global_active_signals = active_signals.copy()
            global_successful_signals = successful_signals.copy()
            global_failed_signals = failed_signals.copy()
            global_positions = positions.copy()
            global_stop_cooldown = stop_cooldown.copy()
            global_allowed_users = ALLOWED_USERS.copy()
            global_admin_users = ADMIN_USERS.copy()
            
            save_stats_to_db(stats)
            save_active_signals_to_db(active_signals)
            save_positions_to_db(positions)  # ✅ POZİSYONLARI DA KAYDET

            # İstatistik özeti yazdır
            print(f"📊 İSTATİSTİK ÖZETİ:")
            total_display = stats.get('successful_signals', 0) + stats.get('failed_signals', 0) + stats.get('active_signals_count', 0)
            print(f"   Toplam Sinyal: {total_display}")
            print(f"   Başarılı: {stats['successful_signals']}")
            print(f"   Başarısız: {stats['failed_signals']}")
            print(f"   Aktif Sinyal: {stats['active_signals_count']}")
            print(f"   100$ Yatırım Toplam Kar/Zarar: ${stats['total_profit_loss']:.2f}")
            # Sadece kapanmış işlemler için ortalama kar/zarar
            closed_count = stats['successful_signals'] + stats['failed_signals']
            closed_pl = 0.0
            for s in successful_signals.values():
                closed_pl += s.get('profit_usd', 0)
            for f in failed_signals.values():
                closed_pl += f.get('loss_usd', 0)
            if closed_count > 0:
                success_rate = (stats['successful_signals'] / closed_count) * 100
                print(f"   Başarı Oranı: %{success_rate:.1f}")
            else:
                print(f"   Başarı Oranı: %0.0")
            
            # Yeni sinyal aramaya devam et
            print("🚀 Yeni sinyal aramaya devam ediliyor...")
            
            # Ana döngü tamamlandı - 15 dakika sonra yeni döngü
            print("Tüm coinler kontrol edildi. 15 dakika sonra yeni sinyal arama döngüsü başlayacak...")
            await asyncio.sleep(900)  # 15 dakika (900 saniye)
            
        except Exception as e:
            print(f"Genel hata: {e}")
            await asyncio.sleep(30)  # 30 saniye (çok daha hızlı)

async def monitor_signals():
    print("🚀 Sinyal izleme sistemi başlatıldı! (Veri Karışıklığı Düzeltildi)")
    
    while True:
        try:
            active_signals = load_active_signals_from_db()

            if not active_signals:
                await asyncio.sleep(5)  # MONITOR_SLEEP_EMPTY 
                continue

            positions = load_positions_from_db()
            orphaned_signals = []
            for symbol in list(active_signals.keys()):
                if symbol not in positions:
                    print(f"⚠️ {symbol} → Positions'da yok, aktif sinyallerden kaldırılıyor")
                    orphaned_signals.append(symbol)
                    del active_signals[symbol]
            
            # Orphaned sinyalleri veritabanından da sil
            if orphaned_signals:
                for symbol in orphaned_signals:
                    try:
                        mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                        print(f"✅ {symbol} aktif sinyali veritabanından silindi")
                    except Exception as e:
                        print(f"❌ {symbol} aktif sinyali silinirken hata: {e}")
                
                # Güncellenmiş aktif sinyalleri kaydet
                save_active_signals_to_db(active_signals)
                print(f"✅ {len(orphaned_signals)} tutarsız sinyal temizlendi")
            
            # Eğer temizlik sonrası aktif sinyal kalmadıysa bekle
            if not active_signals:
                await asyncio.sleep(5)  # MONITOR_SLEEP_EMPTY 
                continue

            print(f"🔍 {len(active_signals)} aktif sinyal izleniyor...")
            print(f"🚨 MONITOR DEBUG: Bu fonksiyon çalışıyor!")
            
            # Aktif sinyallerin detaylı durumunu yazdır
            for symbol, signal in active_signals.items():
                try:
                    symbol_entry_price_raw = signal.get('entry_price_float', signal.get('entry_price', 0))
                    symbol_entry_price = float(str(symbol_entry_price_raw).replace('$', '').replace(',', '')) if symbol_entry_price_raw is not None else 0.0
                    
                    if symbol_entry_price <= 0:
                        print(f"⚠️ {symbol}: Geçersiz giriş fiyatı ({symbol_entry_price}), sinyal atlanıyor")
                        continue

                    try:
                        ticker = client.futures_ticker(symbol=symbol)
                        current_price = float(ticker['lastPrice'])
                    except Exception as e:
                        current_price_raw = signal.get('current_price_float', symbol_entry_price)
                        current_price = float(str(current_price_raw).replace('$', '').replace(',', '')) if current_price_raw is not None else symbol_entry_price
                        print(f"   ⚠️ {symbol}: Gerçek zamanlı fiyat alınamadı, kayıtlı değer kullanılıyor: ${current_price:.6f} (Hata: {e})")
                    
                    target_price_raw = signal.get('target_price', 0)
                    stop_price_raw = signal.get('stop_loss', 0)
                    
                    target_price = float(str(target_price_raw).replace('$', '').replace(',', '')) if target_price_raw is not None else 0.0
                    stop_price = float(str(stop_price_raw).replace('$', '').replace(',', '')) if stop_price_raw is not None else 0.0
                    
                    if target_price <= 0 or stop_price <= 0:
                        print(f"⚠️ {symbol}: Geçersiz hedef/stop fiyatları (T:{target_price}, S:{stop_price}), sinyal atlanıyor")
                        continue
                    
                    signal_type = str(signal.get('type', 'ALIŞ'))
                    
                    if symbol_entry_price > 0 and current_price > 0:
                        leverage = signal.get('leverage', 10)  # Varsayılan 10x
    
                        if signal_type == "ALIŞ" or signal_type == "ALIS":
                            change_percent = ((current_price - symbol_entry_price) / symbol_entry_price) * 100
                            target_distance = ((target_price - current_price) / current_price) * 100
                            stop_distance = ((current_price - stop_price) / current_price) * 100

                        else:  # SATIŞ veya SATIS
                            change_percent = ((symbol_entry_price - current_price) / symbol_entry_price) * 100
                            target_distance = ((current_price - target_price) / current_price) * 100
                            stop_distance = ((stop_price - current_price) / current_price) * 100
                        
                        investment_amount = 100 
                        actual_investment = investment_amount * leverage 
                        profit_loss_usd = (actual_investment * change_percent) / 100

                        if signal_type == "ALIŞ" or signal_type == "ALIS":
                            if change_percent >= 0:
                                print(f"   🟢 {symbol} (ALIŞ): Giriş: ${symbol_entry_price:.6f} → Güncel: ${current_price:.6f} (+{change_percent:.2f}%)")
                            else:
                                print(f"   🔴 {symbol} (ALIŞ): Giriş: ${symbol_entry_price:.6f} → Güncel: ${current_price:.6f} ({change_percent:.2f}%)")
                            
                        else:  # SATIŞ veya SATIS
                            if change_percent >= 0:
                                print(f"   🟢 {symbol} (SATIŞ): Giriş: ${symbol_entry_price:.6f} → Güncel: ${current_price:.6f} (+{change_percent:.2f}%)")
                            else:
                                print(f"   🔴 {symbol} (SATIŞ): Giriş: ${symbol_entry_price:.6f} → Güncel: ${current_price:.6f} ({change_percent:.2f}%)")
                        
                except Exception as e:
                    print(f"   ⚪ {symbol}: Durum hesaplanamadı - Hata: {e}")
            
            for symbol, signal in list(active_signals.items()):
                try:
                    if not mongo_collection.find_one({"_id": f"active_signal_{symbol}"}):
                        print(f"ℹ️ {symbol} sinyali DB'de bulunamadı, bellekten kaldırılıyor.")
                        del active_signals[symbol]
                        continue

                    signal_status = signal.get("status", "pending")
                    if signal_status != "active":
                        print(f"ℹ️ {symbol} sinyali henüz aktif değil (durum: {signal_status}), atlanıyor.")
                        continue

                    symbol_entry_price_raw = signal.get('entry_price_float', signal.get('entry_price', 0))
                    symbol_entry_price = float(str(symbol_entry_price_raw).replace('$', '').replace(',', '')) if symbol_entry_price_raw is not None else 0.0
                    symbol_target_price = float(str(signal.get('target_price', 0)).replace('$', '').replace(',', ''))
                    symbol_stop_loss_price = float(str(signal.get('stop_loss', 0)).replace('$', '').replace(',', ''))
                    symbol_signal_type = signal.get('type', 'ALIŞ')
                                            
                    # 3. ANLIK FİYAT KONTROLÜ
                    try:
                        ticker = client.futures_ticker(symbol=symbol)
                        last_price = float(ticker['lastPrice'])
                        is_triggered_realtime = False
                        trigger_type_realtime = None
                        final_price_realtime = None
                        min_trigger_diff = 0.001  # %0.1 minimum fark

                        if symbol_signal_type == "ALIŞ" or symbol_signal_type == "ALIS":
                            # ALIŞ pozisyonu için kapanış koşulları
                            if last_price >= symbol_target_price and (last_price - symbol_target_price) >= (symbol_target_price * min_trigger_diff):
                                is_triggered_realtime = True
                                trigger_type_realtime = "take_profit"
                                final_price_realtime = last_price
                                print(f"✅ {symbol} - TP tetiklendi: ${last_price:.6f} >= ${symbol_target_price:.6f}")
                            elif last_price <= symbol_stop_loss_price and (symbol_stop_loss_price - last_price) >= (symbol_stop_loss_price * min_trigger_diff):
                                is_triggered_realtime = True
                                trigger_type_realtime = "stop_loss"
                                final_price_realtime = last_price
                                print(f"❌ {symbol} - SL tetiklendi: ${last_price:.6f} <= ${symbol_stop_loss_price:.6f}")
                        elif symbol_signal_type == "SATIŞ" or symbol_signal_type == "SATIS":
                            # SATIŞ pozisyonu için kapanış koşulları
                            if last_price <= symbol_target_price and (symbol_target_price - last_price) >= (symbol_target_price * min_trigger_diff):
                                is_triggered_realtime = True
                                trigger_type_realtime = "take_profit"
                                final_price_realtime = last_price
                                print(f"✅ {symbol} - TP tetiklendi: ${last_price:.6f} <= ${symbol_target_price:.6f}")
                            elif last_price >= symbol_stop_loss_price and (last_price - symbol_stop_loss_price) >= (symbol_stop_loss_price * min_trigger_diff):
                                is_triggered_realtime = True
                                trigger_type_realtime = "stop_loss"
                                final_price_realtime = last_price
                                print(f"❌ {symbol} - SL tetiklendi: ${last_price:.6f} >= ${symbol_stop_loss_price:.6f}")
                        
                        # 4. POZİSYON KAPATMA İŞLEMİ
                        if is_triggered_realtime:
                            print(f"💥 ANLIK TETİKLENDİ: {symbol}, Tip: {trigger_type_realtime}, Fiyat: {final_price_realtime}")
                            update_position_status_atomic(symbol, "closing", {"trigger_type": trigger_type_realtime, "final_price": final_price_realtime})
                            
                            position_data = load_position_from_db(symbol)
                            if position_data:
                                if position_data.get('open_price', 0) <= 0:
                                    print(f"⚠️ {symbol} - Geçersiz pozisyon verileri, pozisyon temizleniyor")
                                    mongo_collection.delete_one({"_id": f"position_{symbol}"})
                                    mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                                    active_signals.pop(symbol, None)
                                    continue
                            else:
                                print(f"❌ {symbol} pozisyon verisi yüklenemedi!")
                                continue
                            
                            await close_position(symbol, trigger_type_realtime, final_price_realtime, signal, position_data)
                            active_signals.pop(symbol, None) # Bellekten de sil
                            continue # Bu sembol bitti, sonraki sinyale geç.
                            
                    except Exception as e:
                        print(f"⚠️ {symbol} - Anlık ticker fiyatı alınamadı: {e}")
                    
                    try:
                        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1m&limit=100"
                        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
                            klines = await api_request_with_retry(session, url, ssl=False)
                        
                    except Exception as e:
                        print(f"⚠️ {symbol} - Mum verisi alınamadı (retry sonrası): {e}")
                        continue

                    if not klines:
                        continue
                    
                    is_triggered, trigger_type, final_price = check_klines_for_trigger(signal, klines)
                    
                    if is_triggered:
                        print(f"💥 MUM TETİKLEDİ: {symbol}, Tip: {trigger_type}, Fiyat: {final_price}")
                        update_position_status_atomic(symbol, "closing", {"trigger_type": trigger_type, "final_price": final_price})
                        position_data = load_position_from_db(symbol)

                        if position_data:
                            if position_data.get('open_price', 0) <= 0:
                                print(f"⚠️ {symbol} - Geçersiz pozisyon verileri, pozisyon temizleniyor")
                                # Geçersiz pozisyonu temizle
                                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                                del active_signals[symbol]
                                continue
                        else:
                            print(f"❌ {symbol} pozisyon verisi yüklenemedi!")
                            continue
                        
                        await close_position(symbol, trigger_type, final_price, signal, position_data)
                        del active_signals[symbol]
                        
                        print(f"✅ {symbol} izleme listesinden kaldırıldı. Bir sonraki sinyale geçiliyor.")
                        continue # Bir sonraki sinyale geç
                    else:
                        # Tetikleme yoksa, anlık fiyatı güncelle
                        if final_price:
                            active_signals[symbol]['current_price'] = format_price(final_price, signal.get('entry_price_float'))
                            active_signals[symbol]['current_price_float'] = final_price
                            active_signals[symbol]['last_update'] = str(datetime.now())
                            
                            # Max/Min değerleri güncelle
                            if "max_price" not in active_signals[symbol] or final_price > active_signals[symbol]["max_price"]:
                                active_signals[symbol]["max_price"] = final_price
                            if "min_price" not in active_signals[symbol] or final_price < active_signals[symbol]["min_price"]:
                                active_signals[symbol]["min_price"] = final_price
                            
                            # Aktif sinyalleri veritabanına kaydet
                            save_active_signals_to_db(active_signals)
                        else:
                            # Tetikleme yoksa pozisyon hala aktif
                            print(f"🔍 {symbol} - Mum verisi ile pozisyon hala aktif")
                    
                except Exception as e:
                    print(f"❌ {symbol} sinyali işlenirken döngü içinde hata oluştu: {e}")
                    if symbol in active_signals:
                        del active_signals[symbol]
                    continue

            await asyncio.sleep(3) # MONITOR_LOOP_SLEEP_SECONDS - Daha hızlı kontrol için 3 saniye
        
        except Exception as e:
            print(f"❌ Ana sinyal izleme döngüsü hatası: {e}")
            await asyncio.sleep(10)  # MONITOR_SLEEP_ERROR - Hata durumunda bekle
            active_signals = load_active_signals_from_db()

async def web_server():
    """Render için basit web sunucusu"""
    app = web.Application()
    
    async def health_check(request):
        return web.Response(text="Bot is running!", content_type='text/plain')
    
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    port = int(os.environ.get('PORT', 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Web sunucusu port {port}'de başlatıldı")
    
    return runner

async def main():
    load_allowed_users()
    await setup_bot()
    await app.initialize()
    await app.start()
    
    # MongoDB'deki bozuk pozisyon verilerini temizle
    cleanup_corrupted_positions()
    
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("✅ Webhook'lar temizlendi")
        await asyncio.sleep(2)  # Biraz bekle
    except Exception as e:
        print(f"Webhook temizleme hatası: {e}")
    
    # Web sunucusunu başlat
    web_runner = await web_server()
    
    # Bot polling'i başlat
    try:
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query", "chat_member", "my_chat_member", "channel_post"])
    except Exception as e:
        print(f"Bot polling hatası: {e}")

    signal_task = asyncio.create_task(signal_processing_loop())
    monitor_task = asyncio.create_task(monitor_signals())
    try:
        # Tüm task'ları bekle
        await asyncio.gather(signal_task, monitor_task)
    except KeyboardInterrupt:
        print("\n⚠️ Bot kapatılıyor...")
    except asyncio.CancelledError:
        print("\nℹ️ Bot task'ları iptal edildi (normal kapatma)")
    finally:
        # Task'ları iptal et
        if not signal_task.done():
            signal_task.cancel()
        if not monitor_task.done():
            monitor_task.cancel()
        
        try:
            await asyncio.gather(signal_task, monitor_task, return_exceptions=True)
        except Exception:
            pass

        try:
            await app.updater.stop()
            print("✅ Telegram bot polling durduruldu")
        except Exception as e:
            print(f"⚠️ Bot polling durdurma hatası: {e}")

        try:
            await app.stop()
            await app.shutdown()
            print("✅ Telegram uygulaması kapatıldı")
        except Exception as e:
            print(f"⚠️ Uygulama kapatma hatası: {e}")
        
        # Web sunucusunu kapat
        await web_runner.cleanup()
        print("✅ Web sunucusu kapatıldı")
        
        close_mongodb()
        print("✅ MongoDB bağlantısı kapatıldı")

def clear_previous_signals_from_db():
    """MongoDB'deki tüm önceki sinyal kayıtlarını ve işaret dokümanını siler."""
    try:
        deleted_count = clear_data_by_pattern("^previous_signal_", "önceki sinyal")
        init_deleted = clear_specific_document("previous_signals_initialized", "initialized bayrağı")
        
        print(f"🧹 MongoDB'den {deleted_count} önceki sinyal silindi; initialized={init_deleted}")
        return deleted_count, init_deleted
    except Exception as e:
        print(f"❌ MongoDB'den önceki sinyaller silinirken hata: {e}")
        return 0, False

def clear_position_data_from_db():
    """MongoDB'deki position_ ile başlayan tüm kayıtları siler (clear_positions.py'den uyarlandı)."""
    try:
        deleted_count = clear_data_by_pattern("^position_", "pozisyon")
        return deleted_count
    except Exception as e:
        print(f"❌ MongoDB'den pozisyonlar silinirken hata: {e}")
        return 0

async def clear_all_command(update, context):
    """Tüm verileri temizler: pozisyonlar, aktif sinyaller, önceki sinyaller, bekleyen kuyruklar, istatistikler (sadece bot sahibi)"""
    user_id, is_authorized = validate_user_command(update, require_owner=True)
    if not is_authorized:
        return
    
    await send_command_response(update, "🧹 Tüm veriler temizleniyor...")
    try:
        # 1) Pozisyonları temizle
        pos_deleted = clear_position_data_from_db()
        
        # 2) Aktif sinyalleri temizle - daha güçlü temizleme
        active_deleted = clear_data_by_pattern("^active_signal_", "aktif sinyal")
        
        # 3) Kalan aktif sinyalleri manuel olarak kontrol et ve sil
        try:
            remaining_active = mongo_collection.find({"_id": {"$regex": "^active_signal_"}})
            remaining_count = 0
            for doc in remaining_active:
                mongo_collection.delete_one({"_id": doc["_id"]})
                remaining_count += 1
            if remaining_count > 0:
                print(f"🧹 Manuel olarak {remaining_count} kalan aktif sinyal silindi")
                active_deleted += remaining_count
        except Exception as e:
            print(f"⚠️ Manuel aktif sinyal temizleme hatası: {e}")
        
        # 4) Global değişkenleri temizle
        global global_active_signals
        global_active_signals = {}
        
        # Boş aktif sinyal listesi kaydet - bu artık tüm dokümanları silecek
        save_active_signals_to_db({})
        
        cooldown_deleted = clear_data_by_pattern("^stop_cooldown_", "stop cooldown")
        
        # 5.5) Sinyal cooldown'ları temizle
        signal_cooldown_deleted = clear_data_by_pattern("^signal_cooldown_", "sinyal cooldown")
        
        # 6) JSON dosyasını da temizle
        try:
            with open('active_signals.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "active_signals": {},
                    "count": 0,
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        
        prev_deleted, init_deleted = clear_previous_signals_from_db()
        global global_waiting_signals

        try:
            global_waiting_signals = {}
        except NameError:
            pass
        
        new_stats = {
            "total_signals": 0,
            "successful_signals": 0,
            "failed_signals": 0,
            "total_profit_loss": 0.0,
            "active_signals_count": 0,
            "tracked_coins_count": 0,
        }
        save_stats_to_db(new_stats)
        global global_stats
        if isinstance(global_stats, dict):
            global_stats.clear()
            global_stats.update(new_stats)
        else:
            global_stats = new_stats
        
        # Son kontrol - kalan dokümanları say
        try:
            final_positions = mongo_collection.count_documents({"_id": {"$regex": "^position_"}})
            final_active = mongo_collection.count_documents({"_id": {"$regex": "^active_signal_"}})
            final_cooldown = mongo_collection.count_documents({"_id": {"$regex": "^stop_cooldown_"}})
            final_signal_cooldown = mongo_collection.count_documents({"_id": {"$regex": "^signal_cooldown_"}})
            
            print(f"🔍 Temizleme sonrası kontrol:")
            print(f"   Kalan pozisyon: {final_positions}")
            print(f"   Kalan aktif sinyal: {final_active}")
            print(f"   Kalan stop cooldown: {final_cooldown}")
            print(f"   Kalan sinyal cooldown: {final_signal_cooldown}")
            
        except Exception as e:
            print(f"⚠️ Son kontrol hatası: {e}")
        
        # Özet mesaj
        summary = (
            f"✅ Temizleme tamamlandı.\n"
            f"• Pozisyon: {pos_deleted} silindi\n"
            f"• Aktif sinyal: {active_deleted} silindi\n"
            f"• Stop cooldown: {cooldown_deleted} silindi\n"
            f"• Sinyal cooldown: {signal_cooldown_deleted} silindi\n"
            f"• Önceki sinyal: {prev_deleted} silindi (initialized: {'silindi' if init_deleted else 'yok'})\n"
            f"• Bekleyen kuyruklar sıfırlandı\n"
            f"• İstatistikler sıfırlandı"
        )
        await send_command_response(update, summary)
    except Exception as e:
        await send_command_response(update, f"❌ ClearAll hatası: {e}")

async def migrate_command(update, context):
    """Eski pozisyonları yeni TP/SL sistemine uyarlama komutu (sadece bot sahibi)"""
    user_id, is_authorized = validate_user_command(update, require_owner=True)
    if not is_authorized:
        return
    
    await send_command_response(update, "🔄 Eski pozisyonlar yeni TP/SL sistemine uyarlanıyor...")
    
    try:
        await migrate_old_positions_to_new_tp_sl_system()
        await send_command_response(update, "✅ Migration tamamlandı! Eski pozisyonlar yeni sisteme uyarlandı.")
    except Exception as e:
        await send_command_response(update, f"❌ Migration hatası: {e}")
        print(f"❌ Migration hatası: {e}")

async def migrate_old_positions_to_new_tp_sl_system():
    """Eski pozisyonları yeni TP/SL sistemine uyarlar"""
    try:
        print("🔄 Eski pozisyonlar yeni TP/SL sistemine uyarlanıyor...")
        
        # Aktif sinyalleri yükle
        active_signals = load_active_signals_from_db()
        if not active_signals:
            print("ℹ️ Aktif sinyal bulunamadı")
            return
        
        migrated_count = 0
        for symbol, signal in active_signals.items():
            try:
                # Pozisyon tipini kontrol et
                signal_type = signal.get('type', 'ALIŞ')
                entry_price = float(signal.get('entry_price_float', 0))
                
                if entry_price <= 0:
                    print(f"⚠️ {symbol} - Geçersiz giriş fiyatı, atlanıyor")
                    continue
                
                print(f"🔍 {symbol} pozisyonu kontrol ediliyor...")
                
                # Kripto özel ayarları al
                if symbol not in CRYPTO_SETTINGS:
                    print(f"⚠️ {symbol} - Kripto özel ayarları bulunamadı, atlanıyor")
                    continue
                
                crypto_config = CRYPTO_SETTINGS[symbol]
                tp_percent = crypto_config["tp_percent"]
                sl_percent = crypto_config["sl_percent"]
                leverage = crypto_config["leverage"]
                
                # Yeni tek TP/SL hesaplamaları
                if signal_type == "ALIŞ" or signal_type == "ALIS":
                    # ALIŞ pozisyonu için
                    target_price = entry_price * (1 + tp_percent / 100)
                    stop_loss = entry_price * (1 - sl_percent / 100)
                    
                    print(f"   Hedef: ${target_price:.6f} (%{tp_percent}), Stop: ${stop_loss:.6f} (%{sl_percent})")
                    
                elif signal_type == "SATIŞ" or signal_type == "SATIS":
                    # SATIŞ pozisyonu için
                    target_price = entry_price * (1 - tp_percent / 100)
                    stop_loss = entry_price * (1 + sl_percent / 100)
                    
                    print(f"   Hedef: ${target_price:.6f} (%{tp_percent}), Stop: ${stop_loss:.6f} (%{sl_percent})")
                
                else:
                    print(f"⚠️ {symbol} - Bilinmeyen pozisyon tipi: {signal_type}")
                    continue
                
                # Güncel fiyatı al
                try:
                    ticker = client.futures_ticker(symbol=symbol)
                    current_price = float(ticker['lastPrice'])
                    print(f"   Güncel fiyat: ${current_price:.6f}")
                except Exception as e:
                    print(f"⚠️ {symbol} - Güncel fiyat alınamadı: {e}")
                    continue
                
                # Hedef veya stop kontrolü
                if signal_type == "ALIŞ" or signal_type == "ALIS":
                    # ALIŞ pozisyonu kontrolü
                    if current_price >= target_price:
                        print(f"🎯 {symbol} HEDEF (%{tp_percent}) GERÇEKLEŞTİ!")
                        
                        profit_percentage = ((target_price - entry_price) / entry_price) * 100
                        profit_usd = (100 * (profit_percentage / 100)) * leverage
                        
                        target_message = f"""🎯 HEDEF GERÇEKLEŞTİ! 🎯

🔹 Kripto Çifti: {symbol}
💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})
📈 Giriş: ${entry_price:.6f}
💵 Çıkış: ${target_price:.6f}
🎯 Hedef Seviyesi: %{tp_percent}
🏁 Hedef gerçekleşti! Pozisyon kapatılıyor..."""
                        
                        await send_signal_to_all_users(target_message)
                        print(f"✅ {symbol} - HEDEF mesajı gönderildi, pozisyon kapatılıyor")
                        
                        # Pozisyonu kapat
                        await process_position_close(symbol, "take_profit", target_price, {}, {}, {}, {}, "HEDEF")
                        continue
                    

                    # Stop kontrolü
                    if current_price <= stop_loss:
                        print(f"🛑 {symbol} STOP (%{sl_percent}) GERÇEKLEŞTİ!")
                        
                        loss_percentage = ((entry_price - stop_loss) / entry_price) * 100
                        loss_usd = (100 * (loss_percentage / 100)) * leverage
                        
                        stop_message = f"""🛑 STOP GERÇEKLEŞTİ! 🛑

🔹 Kripto Çifti: {symbol}
💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})
📈 Giriş: ${entry_price:.6f}
💵 Çıkış: ${stop_loss:.6f}
🏁 Stop tetiklendi! Pozisyon kapatılıyor..."""
                        
                        await send_signal_to_all_users(stop_message)
                        print(f"✅ {symbol} - STOP mesajı gönderildi, pozisyon kapatılıyor")
                        
                        # Pozisyonu kapat
                        await process_position_close(symbol, "stop_loss", stop_loss, {}, {}, {}, {}, "STOP")
                        continue
                
                elif signal_type == "SATIŞ" or signal_type == "SATIS":
                    # SATIŞ pozisyonu kontrolü
                    if current_price <= target_price:
                        print(f"🎯 {symbol} SATIŞ HEDEF (%{tp_percent}) GERÇEKLEŞTİ!")
                        
                        profit_percentage = ((entry_price - target_price) / entry_price) * 100
                        profit_usd = (100 * (profit_percentage / 100)) * leverage
                        
                        target_message = f"""🎯 SATIŞ HEDEF GERÇEKLEŞTİ! 🎯

🔹 Kripto Çifti: {symbol}
💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})
📈 Giriş: ${entry_price:.6f}
💵 Çıkış: ${target_price:.6f}
🎯 Hedef Seviyesi: %{tp_percent}
🏁 Hedef gerçekleşti! Pozisyon kapatılıyor..."""
                        
                        await send_signal_to_all_users(target_message)
                        print(f"✅ {symbol} - SATIŞ HEDEF mesajı gönderildi, pozisyon kapatılıyor")
                        
                        # Pozisyonu kapat
                        await process_position_close(symbol, "take_profit", target_price, {}, {}, {}, {}, "HEDEF")
                        continue
                    
                    # Stop kontrolü
                    if current_price >= stop_loss:
                        print(f"🛑 {symbol} SATIŞ STOP (%{sl_percent}) GERÇEKLEŞTİ!")
                        
                        loss_percentage = ((stop_loss - entry_price) / entry_price) * 100
                        loss_usd = (100 * (loss_percentage / 100)) * leverage
                        
                        stop_message = f"""🛑 SATIŞ STOP GERÇEKLEŞTİ! 🛑

🔹 Kripto Çifti: {symbol}
💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})
📈 Giriş: ${entry_price:.6f}
💵 Çıkış: ${stop_loss:.6f}
🏁 Stop tetiklendi! Pozisyon kapatılıyor..."""
                        
                        await send_signal_to_all_users(stop_message)
                        print(f"✅ {symbol} - SATIŞ STOP mesajı gönderildi, pozisyon kapatılıyor")
                        
                        # Pozisyonu kapat
                        await process_position_close(symbol, "stop_loss", stop_loss, {}, {}, {}, {}, "STOP")
                        continue
                
                migrated_count += 1
                print(f"✅ {symbol} pozisyonu yeni sisteme uyarlandı")
                
            except Exception as e:
                print(f"❌ {symbol} pozisyonu uyarlanırken hata: {e}")
                continue
        
        print(f"🎯 Migration tamamlandı! {migrated_count} pozisyon yeni sisteme uyarlandı")
        
    except Exception as e:
        print(f"❌ Migration hatası: {e}")

async def calculate_signals_for_symbol(symbol, timeframes, tf_names):
    """Bir sembol için tüm zaman dilimlerinde sinyalleri hesaplar"""
    current_signals = {}
    
    for tf_name in tf_names:
        try:
            df = await async_get_historical_data(symbol, timeframes[tf_name], 1000)
            if df is None or df.empty:
                return None
            
            df = calculate_full_pine_signals(df, tf_name)
            closest_idx = -1  # Son mum
            signal = int(df.iloc[closest_idx]['signal'])
            
            if signal == 0:
                # Eğer signal 0 ise, MACD ile düzelt
                if df['macd'].iloc[closest_idx] > df['macd_signal'].iloc[closest_idx]:
                    signal = 1
                else:
                    signal = -1
            current_signals[tf_name] = signal
            
        except Exception as e:
            print(f"❌ {symbol} {tf_name} sinyal hesaplama hatası: {e}")
            return None
    
    return current_signals

def calculate_signal_counts(signals, tf_names):
    """Sinyal sayılarını hesaplar"""
    signal_values = [signals.get(tf, 0) for tf in tf_names]
    buy_count = sum(1 for s in signal_values if s == 1)
    sell_count = sum(1 for s in signal_values if s == -1)
    
    print(f"🔍 Sinyal sayımı: {tf_names}")
    print(f"   Sinyal değerleri: {signal_values}")
    print(f"   ALIŞ sayısı: {buy_count}, SATIŞ sayısı: {sell_count}")
    return buy_count, sell_count

def check_2_2_rule(buy_count, sell_count):
    """15m+30m 2/2 kuralını kontrol eder - her iki zaman dilimi aynı yönde olmalı"""
    result = buy_count == 2 or sell_count == 2
    print(f"🔍 15m+30m 2/2 kural kontrolü: ALIŞ={buy_count}, SATIŞ={sell_count} → Sonuç: {result}")
    return result

def check_cooldown(symbol, cooldown_dict, hours=4):  # ✅ 4 SAAT COOLDOWN - TÜM SİNYALLER İÇİN
    """Cooldown kontrolü yapar - tüm sinyaller için 4 saat"""
    if symbol in cooldown_dict:
        last_time = cooldown_dict[symbol]
        if isinstance(last_time, str):
            last_time = datetime.fromisoformat(last_time)
        time_diff = (datetime.now() - last_time).total_seconds() / 3600
        if time_diff < hours:
            remaining_hours = hours - time_diff
            remaining_minutes = (remaining_hours - int(remaining_hours)) * 60
            end_time = (last_time + timedelta(hours=hours)).strftime('%H:%M:%S')
            print(f"⏰ {symbol} → Cooldown aktif: {int(remaining_hours)}s {int(remaining_minutes)}dk kaldı (bitiş: {end_time})")
            print(f"   Son hedef zamanı: {last_time.strftime('%H:%M:%S')}")
            return True  # Cooldown aktif
        else:
            del cooldown_dict[symbol]  # Cooldown doldu
            print(f"✅ {symbol} → Cooldown süresi doldu, yeni sinyal aranabilir")
    return False  # Cooldown yok

def clear_data_by_pattern(pattern, description="veri"):
    """Regex pattern ile eşleşen verileri MongoDB'den siler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print(f"❌ MongoDB bağlantısı kurulamadı, {description} silinemedi")
                return 0
        
        # Önce kaç tane doküman olduğunu kontrol et
        before_count = mongo_collection.count_documents({"_id": {"$regex": pattern}})
        print(f"🔍 {description} temizleme öncesi: {before_count} doküman bulundu")
        
        delete_result = mongo_collection.delete_many({"_id": {"$regex": pattern}})
        deleted_count = getattr(delete_result, "deleted_count", 0)
        
        # Sonra kaç tane kaldığını kontrol et
        after_count = mongo_collection.count_documents({"_id": {"$regex": pattern}})
        print(f"🧹 MongoDB'den {deleted_count} {description} silindi, {after_count} kaldı")
        
        return deleted_count
    except Exception as e:
        print(f"❌ MongoDB'den {description} silinirken hata: {e}")
        return 0

def clear_specific_document(doc_id, description="doküman"):
    """Belirli bir dokümanı MongoDB'den siler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print(f"❌ MongoDB bağlantısı kurulamadı, {description} silinemedi")
                return False
        
        delete_result = mongo_collection.delete_one({"_id": doc_id})
        deleted_count = getattr(delete_result, "deleted_count", 0)
        
        if deleted_count > 0:
            print(f"🧹 MongoDB'den {description} silindi")
            return True
        else:
            print(f"ℹ️ {description} zaten mevcut değildi")
            return False
    except Exception as e:
        print(f"❌ MongoDB'den {description} silinirken hata: {e}")
        return False

def load_data_by_pattern(pattern, data_key="data", description="veri", transform_func=None):
    """Regex pattern ile eşleşen verileri MongoDB'den yükler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print(f"❌ MongoDB bağlantısı kurulamadı, {description} yüklenemedi")
                return {}
        
        result = {}
        docs = mongo_collection.find({"_id": {"$regex": pattern}})
        
        for doc in docs:
            if transform_func:
                result.update(transform_func(doc))
            else:
                # Varsayılan transform: _id'den pattern'i çıkar ve data_key'i al
                key = doc["_id"].replace(pattern.replace("^", "").replace("$", ""), "")
                if data_key and data_key in doc:
                    result[key] = doc[data_key]
                else:
                    result[key] = doc
        
        print(f"✅ MongoDB'den {len(result)} {description} yüklendi")
        return result
    except Exception as e:
        print(f"❌ MongoDB'den {description} yüklenirken hata: {e}")
        return {}

def safe_mongodb_operation(operation_func, error_message="MongoDB işlemi", default_return=None):
    """MongoDB işlemlerini güvenli şekilde yapar"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print(f"❌ MongoDB bağlantısı kurulamadı, {error_message} yapılamadı")
                return default_return
        return operation_func()
    except Exception as e:
        print(f"❌ {error_message} sırasında hata: {e}")
        return default_return
    
def save_stop_cooldown_to_db(stop_cooldown):
    """Stop cooldown verilerini MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, stop cooldown kaydedilemedi")
                return False
        
        # Önce tüm stop cooldown verilerini sil
        mongo_collection.delete_many({"_id": {"$regex": "^stop_cooldown_"}})
        
        # Yeni stop cooldown verilerini ekle
        for symbol, timestamp in stop_cooldown.items():
            doc_id = f"stop_cooldown_{symbol}"
            mongo_collection.insert_one({
                "_id": doc_id,
                "data": timestamp,
                "timestamp": datetime.now()
            })
        
        print(f"✅ {len(stop_cooldown)} stop cooldown MongoDB'ye kaydedildi")
        return True
    except Exception as e:
        print(f"❌ Stop cooldown MongoDB'ye kaydedilirken hata: {e}")
        return False

async def close_position(symbol, trigger_type, final_price, signal, position_data=None):
    print(f"--- Pozisyon Kapatılıyor: {symbol} ({trigger_type}) ---")
    try:
        # Önce veritabanından pozisyonun hala var olduğunu doğrula
        position_doc = mongo_collection.find_one({"_id": f"position_{symbol}"})
        if not position_doc:
            print(f"⚠️ {symbol} pozisyonu zaten kapatılmış veya DB'de bulunamadı. Yinelenen işlem engellendi.")
            # Bellekteki global değişkenlerden de temizle
            global_positions.pop(symbol, None)
            global_active_signals.pop(symbol, None)
            return
        try:
            if position_data:
                entry_price_raw = position_data.get('open_price', 0)
                target_price_raw = position_data.get('target', 0)
                stop_loss_raw = position_data.get('stop', 0)
                entry_price = float(entry_price_raw) if entry_price_raw is not None else 0.0
                target_price = float(target_price_raw) if target_price_raw is not None else 0.0
                stop_loss_price = float(stop_loss_raw) if stop_loss_raw is not None else 0.0
                signal_type = str(position_data.get('type', 'ALIŞ'))
                leverage = int(position_data.get('leverage', 10))
                
            else:
                entry_price_raw = signal.get('entry_price_float', 0)
                target_price_raw = signal.get('target_price', '0')
                stop_loss_raw = signal.get('stop_loss', '0')
                entry_price = float(entry_price_raw) if entry_price_raw is not None else 0.0
                
                # String formatındaki fiyatları temizle
                if isinstance(target_price_raw, str):
                    target_price = float(target_price_raw.replace('$', '').replace(',', ''))
                else:
                    target_price = float(target_price_raw) if target_price_raw is not None else 0.0
                
                if isinstance(stop_loss_raw, str):
                    stop_loss_price = float(stop_loss_raw.replace('$', '').replace(',', ''))
                else:
                    stop_loss_price = float(stop_loss_raw) if stop_loss_raw is not None else 0.0
                
                signal_type = str(signal.get('type', 'ALIŞ'))
                leverage = int(signal.get('leverage', 10))
                        
        except (ValueError, TypeError) as e:
            print(f"❌ {symbol} - Pozisyon verisi dönüşüm hatası: {e}")
            print(f"   position_data: {position_data}")
            print(f"   signal: {signal}")
            # Hatalı pozisyonu temizle
            mongo_collection.delete_one({"_id": f"position_{symbol}"})
            mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
            global_positions.pop(symbol, None)
            global_active_signals.pop(symbol, None)
            return
        
        # Giriş fiyatı 0 ise pozisyonu temizle ve çık
        if entry_price <= 0:
            print(f"⚠️ {symbol} - Geçersiz giriş fiyatı ({entry_price}), pozisyon temizleniyor")
            # Pozisyonu veritabanından sil
            mongo_collection.delete_one({"_id": f"position_{symbol}"})
            mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
            # Bellekteki global değişkenlerden de temizle
            global_positions.pop(symbol, None)
            global_active_signals.pop(symbol, None)
            return
        
        # trigger_type None ise pozisyon durumunu analiz et ve otomatik belirle
        if trigger_type is None:
            print(f"🔍 {symbol} - trigger_type None, pozisyon durumu analiz ediliyor...")
            
            # Güncel fiyat bilgisini al
            try:
                df1m = await async_get_historical_data(symbol, '1m', 1)
                if df1m is not None and not df1m.empty:
                    current_price = float(df1m['close'].iloc[-1])
                    print(f"🔍 {symbol} - Güncel fiyat: ${current_price:.6f}")
                    
                    # Pozisyon tipine göre hedef ve stop kontrolü
                    if signal_type == "ALIŞ" or signal_type == "ALIS":
                        # ALIŞ pozisyonu için
                        if current_price >= target_price:
                            trigger_type = "take_profit"
                            final_price = current_price
                            print(f"✅ {symbol} - Otomatik TP tespit edildi: ${current_price:.6f} >= ${target_price:.6f}")
                        elif current_price <= stop_loss_price:
                            trigger_type = "stop_loss"
                            final_price = current_price
                            print(f"❌ {symbol} - Otomatik SL tespit edildi: ${current_price:.6f} <= ${stop_loss_price:.6f}")
                        else:
                            # Pozisyon hala aktif, varsayılan olarak TP kabul et
                            trigger_type = "take_profit"
                            final_price = target_price
                            print(f"⚠️ {symbol} - Pozisyon hala aktif, varsayılan TP: ${target_price:.6f}")
                    
                    elif signal_type == "SATIŞ" or signal_type == "SATIS":
                        # SATIŞ pozisyonu için
                        if current_price <= target_price:
                            trigger_type = "take_profit"
                            final_price = current_price
                            print(f"✅ {symbol} - Otomatik TP tespit edildi: ${current_price:.6f} <= ${target_price:.6f}")
                        elif current_price >= stop_loss_price:
                            trigger_type = "stop_loss"
                            final_price = current_price
                            print(f"❌ {symbol} - Otomatik SL tespit edildi: ${current_price:.6f} >= ${stop_loss_price:.6f}")
                        else:
                            # Pozisyon hala aktif, varsayılan olarak TP kabul et
                            trigger_type = "take_profit"
                            final_price = target_price
                            print(f"⚠️ {symbol} - Pozisyon hala aktif, varsayılan TP: ${target_price:.6f}")
                    
                else:
                    # Fiyat bilgisi alınamadı, varsayılan değerler
                    trigger_type = "take_profit"
                    final_price = target_price
                    print(f"⚠️ {symbol} - Fiyat bilgisi alınamadı, varsayılan TP: ${target_price:.6f}")
                    
            except Exception as e:
                print(f"⚠️ {symbol} - Güncel fiyat alınamadı: {e}, varsayılan TP kullanılıyor")
                trigger_type = "take_profit"
                final_price = target_price
        
        # Final price'ı güvenli şekilde dönüştür
        try:
            final_price_float = float(final_price) if final_price is not None else 0.0
        except (ValueError, TypeError) as e:
            print(f"❌ {symbol} - Final price dönüşüm hatası: {e}")
            final_price_float = 0.0
        
        # Kar/Zarar hesaplaması - SL/TP fiyatlarından hesaplama (gerçek piyasa fiyatından değil)
        profit_loss_percent = 0
        if entry_price > 0:
            try:
                if trigger_type == "take_profit":
                    # Take-profit: Hedef fiyatından çıkış (ne kadar yükselirse yükselsin)
                    if signal_type == "ALIŞ" or signal_type == "ALIS":
                        profit_loss_percent = ((target_price - entry_price) / entry_price) * 100
                    else: # SATIŞ veya SATIS
                        profit_loss_percent = ((entry_price - target_price) / entry_price) * 100
                    print(f"🎯 {symbol} - TP hesaplaması: Hedef fiyatından (${target_price:.6f}) çıkış")
                    
                elif trigger_type == "stop_loss":
                    # Stop-loss: Stop fiyatından çıkış (ne kadar düşerse düşsün)
                    if signal_type == "ALIŞ" or signal_type == "ALIS":
                        profit_loss_percent = ((stop_loss_price - entry_price) / entry_price) * 100
                    else: # SATIŞ veya SATIS
                        profit_loss_percent = ((entry_price - stop_loss_price) / entry_price) * 100
                    print(f"🛑 {symbol} - SL hesaplaması: Stop fiyatından (${stop_loss_price:.6f}) çıkış")
                    
                else:
                    # Varsayılan durum (final_price kullan)
                    if signal_type == "ALIŞ" or signal_type == "ALIS":
                        profit_loss_percent = ((final_price_float - entry_price) / entry_price) * 100
                    else: # SATIŞ veya SATIS
                        profit_loss_percent = ((entry_price - final_price_float) / entry_price) * 100
                    print(f"⚠️ {symbol} - Varsayılan hesaplama: Final fiyattan (${final_price_float:.6f}) çıkış")
                 
            except Exception as e:
                print(f"❌ {symbol} - Kâr/zarar hesaplama hatası: {e}")
                profit_loss_percent = 0
        else:
            print(f"⚠️ {symbol} - Geçersiz giriş fiyatı ({entry_price}), kâr/zarar hesaplanamadı")
            profit_loss_percent = 0
        
        profit_loss_usd = (100 * (profit_loss_percent / 100)) * leverage # 100$ ve kaldıraç ile
        
        # İstatistikleri atomik olarak güncelle (Race condition'ları önler)
        print(f"🔍 {symbol} - Pozisyon kapatılıyor: {trigger_type} - ${final_price_float:.6f}")
            
        if trigger_type == "take_profit":
            # Atomik güncelleme ile istatistikleri güncelle
            update_stats_atomic({
                "successful_signals": 1,
                "total_profit_loss": profit_loss_usd
            })
            
            # Take-profit mesajında hedef fiyatından çıkış göster
            exit_price = target_price if trigger_type == "take_profit" else final_price_float
            message = (
                f"🎯 <b>HEDEF OLDU!</b> 🎯\n\n"
                f"🔹 <b>Kripto Çifti:</b> {symbol}\n"
                f"💰 <b>Kar:</b> %{profit_loss_percent:.2f} (${profit_loss_usd:.2f})\n"
                f"📈 <b>Giriş:</b> ${entry_price:.6f}\n"
                f"💵 <b>Çıkış:</b> ${exit_price:.6f}"
            )
            await send_signal_to_all_users(message)
            # Bot sahibine hedef mesajı gönderme
        
        elif trigger_type == "stop_loss":
            # Atomik güncelleme ile istatistikleri güncelle
            update_stats_atomic({
                "failed_signals": 1,
                "total_profit_loss": profit_loss_usd
            })
            
            # Stop-loss mesajında stop fiyatından çıkış göster
            exit_price = stop_loss_price if trigger_type == "stop_loss" else final_price_float
            message = (
                f"🛑 <b>STOP OLDU!</b> 🛑\n\n"
                f"🔹 <b>Kripto Çifti:</b> {symbol}\n"
                f"💸 <b>Zarar:</b> %{profit_loss_percent:.2f} (${profit_loss_usd:.2f})\n"
                f"📈 <b>Giriş:</b> ${entry_price:.6f}\n"
                f"💵 <b>Çıkış:</b> ${exit_price:.6f}"
            )
            # STOP mesajları sadece bot sahibine gidecek
            await send_admin_message(message)
        
        # Pozisyonu veritabanından sil
        mongo_collection.delete_one({"_id": f"position_{symbol}"})
        mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
        
        # Cooldown'a ekle (2 saat)
        global global_stop_cooldown
        global_stop_cooldown[symbol] = datetime.now()
        
        # Cooldown'ı veritabanına kaydet
        save_stop_cooldown_to_db({symbol: datetime.now()})
        
        # Bellekteki global değişkenlerden de temizle
        global_positions.pop(symbol, None)
        global_active_signals.pop(symbol, None)
        
        print(f"✅ {symbol} pozisyonu başarıyla kapatıldı ve 2 saat cooldown'a eklendi")
        
    except Exception as e:
        print(f"❌ {symbol} pozisyon kapatılırken hata: {e}")
        # Hata durumunda da pozisyonu temizlemeye çalış
        try:
            mongo_collection.delete_one({"_id": f"position_{symbol}"})
            mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
            global_positions.pop(symbol, None)
            global_active_signals.pop(symbol, None)
            print(f"✅ {symbol} pozisyonu hata sonrası temizlendi")
        except:
            pass

def cleanup_corrupted_positions():
    """MongoDB'deki bozuk pozisyon verilerini temizler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, bozuk pozisyonlar temizlenemedi")
                return False
        
        print("🧹 Bozuk pozisyon verileri temizleniyor...")
        
        # Tüm pozisyon belgelerini kontrol et
        docs = mongo_collection.find({"_id": {"$regex": "^position_"}})
        corrupted_count = 0
        
        for doc in docs:
            symbol = doc["_id"].replace("position_", "")
            data = doc.get("data", {})
            
            # Kritik alanların varlığını kontrol et
            required_fields = ['type', 'target', 'stop', 'open_price', 'leverage']
            missing_fields = [field for field in required_fields if field not in data]
            
            if missing_fields:
                print(f"⚠️ {symbol} - Eksik alanlar: {missing_fields}, pozisyon siliniyor")
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                corrupted_count += 1
                continue
            
            # Fiyat değerlerinin geçerliliğini kontrol et
            try:
                open_price = float(data['open_price'])
                target_price = float(data['target'])
                stop_price = float(data['stop'])
                
                if open_price <= 0 or target_price <= 0 or stop_price <= 0:
                    print(f"⚠️ {symbol} - Geçersiz fiyat değerleri, pozisyon siliniyor")
                    print(f"   Giriş: {open_price}, Hedef: {target_price}, Stop: {stop_price}")
                    mongo_collection.delete_one({"_id": f"position_{symbol}"})
                    mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                    corrupted_count += 1
                    continue
                    
            except (ValueError, TypeError) as e:
                print(f"⚠️ {symbol} - Fiyat dönüşüm hatası: {e}, pozisyon siliniyor")
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                corrupted_count += 1
                continue
        
        if corrupted_count > 0:
            print(f"✅ {corrupted_count} bozuk pozisyon verisi temizlendi")
        else:
            print("✅ Bozuk pozisyon verisi bulunamadı")
        
        return True
        
    except Exception as e:
        print(f"❌ Bozuk pozisyonlar temizlenirken hata: {e}")
        return False

async def process_position_close(symbol, trigger_type, final_price, positions, active_signals, stats, stop_cooldown, level_name):
    """Pozisyon kapatma işlemini yönetir"""
    try:
        print(f"🔒 {symbol} → {level_name} gerçekleşti! Pozisyon kapatılıyor...")
        
        # İstatistikleri güncelle
        if trigger_type == "take_profit":
            stats["successful_signals"] += 1
            # Kar hesaplaması
            position = positions.get(symbol, {})
            entry_price = float(position.get('open_price', 0))
            if entry_price > 0:
                profit_percentage = ((final_price - entry_price) / entry_price) * 100
                leverage = position.get('leverage', 10)  # Varsayılan 10x
                profit_usd = (100 * (profit_percentage / 100)) * leverage
                stats["total_profit_loss"] += profit_usd
                print(f"💰 {symbol} - Kar: %{profit_percentage:.2f} (${profit_usd:.2f})")
        else:  # stop_loss
            stats["failed_signals"] += 1
            # Zarar hesaplaması
            position = positions.get(symbol, {})
            entry_price = float(position.get('open_price', 0))
            if entry_price > 0:
                loss_percentage = ((entry_price - final_price) / entry_price) * 100
                leverage = position.get('leverage', 10)  # Varsayılan 10x
                loss_usd = (100 * (loss_percentage / 100)) * leverage
                stats["total_profit_loss"] -= loss_usd
                print(f"💸 {symbol} - Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})")
        
        # Cooldown'a ekle (2 saat)
        cooldown_time = datetime.now()
        stop_cooldown[symbol] = cooldown_time
        print(f"🔒 {symbol} → {level_name} Cooldown'a eklendi: {cooldown_time.strftime('%H:%M:%S')}")
        print(f"   Cooldown süresi: 2 saat → Bitiş: {(cooldown_time + timedelta(hours=2)).strftime('%H:%M:%S')}")
        save_stop_cooldown_to_db(stop_cooldown)
        
        # Pozisyon ve aktif sinyali kaldır
        del positions[symbol]
        if symbol in active_signals:
            del active_signals[symbol]
        
        # Veritabanı kayıtlarını kontrol et
        positions_saved = save_positions_to_db(positions)
        active_signals_saved = save_active_signals_to_db(active_signals)
        
        if not positions_saved or not active_signals_saved:
            print(f"⚠️ {symbol} veritabanı kaydı başarısız! Pozisyon: {positions_saved}, Aktif Sinyal: {active_signals_saved}")
            # Hata durumunda tekrar dene
            await asyncio.sleep(1)
            positions_saved = save_positions_to_db(positions)
            active_signals_saved = save_active_signals_to_db(active_signals)
            if not positions_saved or not active_signals_saved:
                print(f"❌ {symbol} veritabanı kaydı ikinci denemede de başarısız!")
        else:
            print(f"✅ {symbol} veritabanından başarıyla kaldırıldı")
        
        print(f"✅ {symbol} - {level_name} tespit edildi ve işlendi!")
        
    except Exception as e:
        print(f"❌ {symbol} pozisyon kapatma hatası: {e}")

if __name__ == "__main__":
    asyncio.run(main())