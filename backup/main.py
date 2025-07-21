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

# Daftar database yang ingin dibackup
databases = [
    {"name": "ccgnimex", "user": "ccgnimex", "password": "aaaaaaac"},
    {"name": "ginvite", "user": "ginvite", "password": "aaaaaaac"}
]

# Inisiasi client Pyrogram
app = Client("backup_bot", api_id=app_id, api_hash=app_hash, bot_token=bot_token)

# Fungsi untuk membackup database MySQL menggunakan mysqldump
def backup_database(db_name, user, password):
    backup_file = f"{db_name}_backup_" + time.strftime("%Y%m%d-%H%M%S") + ".sql"
    try:
        subprocess.run(
            ["mysqldump", "-u", user, f"-p{password}", db_name],
            stdout=open(backup_file, 'w'),
            check=True
        )
        return backup_file
    except subprocess.CalledProcessError as e:
        print(f"Error ketika membackup database {db_name}: {e}")
        return None

# Fungsi untuk mengirim backup ke grup Telegram
def send_backup(backup_file):
    with app:
        try:
            app.send_document(group_id, document=backup_file, caption=f"Backup file: {backup_file}")
        except FloodWait as e:
            print(f"FloodWait error: Menunggu {e.x} detik sebelum mengirim lagi.")
            time.sleep(e.x)

# Jalankan pengiriman backup setiap 1 menit
while True:
    for db in databases:
        backup_file = backup_database(db["name"], db["user"], db["password"])
        if backup_file:
            send_backup(backup_file)
            os.remove(backup_file)
    time.sleep(60)  # Tunggu 1 menit sebelum membackup ulang
