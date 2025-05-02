import os
import time
from pyrogram import Client
from pyrogram.errors import FloodWait
import subprocess

# Konfigurasi Pyrogram
app_id = 11245554
app_hash = "0c43822f3a128287db0ee9c74ad02d8f"
bot_token = "5239057973:AAEFjxIVnXmeEnjqaaObmLTkMQMRKTW5OWs"
group_id = -1001559315851

# Konfigurasi Database MySQL
mysql_user = "ccgnimex"  # Ganti dengan username MySQL
mysql_password = "aaaaaaac"  # Ganti dengan password MySQL
mysql_database = "ccgnimex"  # Ganti dengan nama database yang ingin di-backup

# Inisiasi client Pyrogram
app = Client("backup_bot", api_id=app_id, api_hash=app_hash, bot_token=bot_token)

# Fungsi untuk membackup database MySQL menggunakan mysqldump
def backup_database():
    # Nama file backup
    backup_file = "backup_" + time.strftime("%Y%m%d-%H%M%S") + ".sql"
    
    # Jalankan perintah mysqldump untuk backup database
    try:
        subprocess.run(
            ["mysqldump", "-u", mysql_user, f"-p{mysql_password}", mysql_database],
            stdout=open(backup_file, 'w'),
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"Error ketika membuat backup: {e}")
    
    return backup_file

# Fungsi untuk mengirim backup ke grup Telegram
def send_backup(backup_file):
    with app:
        try:
            app.send_document(group_id, document=backup_file, caption="Backup Database MySQL")
        except FloodWait as e:
            print(f"FloodWait error: Menunggu {e.x} detik sebelum mengirim lagi.")
            time.sleep(e.x)

# Jalankan pengiriman backup setiap 1 menit
while True:
    backup_file = backup_database()  # Membuat backup
    send_backup(backup_file)  # Kirim ke grup Telegram
    os.remove(backup_file)  # Hapus file backup setelah dikirim
    time.sleep(60)  # Tunggu 1 menit sebelum membuat backup baru