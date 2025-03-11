import os
import re
import requests
import zipfile
import rarfile
import logging
from pyrogram import Client, filters
import boto3
import magic
import mimetypes
from botocore.exceptions import NoCredentialsError, PartialCredentialsError
import asyncio
import signal
import gdown
from collections import defaultdict
import mysql.connector
from urllib.parse import quote
import httpx
import aiofiles


# Dictionary untuk menyimpan antrian tugas pengguna
user_task_queues = defaultdict(list)
user_progress_messages = defaultdict(lambda: None)

def insert_into_sql(anime_id, episode_number, title, video_url, resolusi="en"):
    try:
        # Koneksi ke MySQL
        conn = mysql.connector.connect(
            host="localhost",
            user="ccgnimex",
            password="aaaaaaac",
            database="ccgnimex"
        )
        cursor = conn.cursor()

        # Query untuk REPLACE data jika sudah ada
        query = """
        INSERT INTO nonton (anime_id, episode_number, title, video_url, resolusi)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            title = VALUES(title),
            video_url = VALUES(video_url),
            resolusi = VALUES(resolusi)
        """
        cursor.execute(query, (anime_id, episode_number, title, video_url, resolusi))
        conn.commit()

        logging.info(f"Data episode {episode_number} berhasil dimasukkan atau diperbarui ke database.")
    except mysql.connector.Error as e:
        logging.error(f"Gagal memasukkan data ke database MySQL: {e}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()



# Konfigurasi logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

# Konfigurasi boto3
B2_ENDPOINT_URL = 'https://s3.us-east-005.backblazeb2.com'
B2_ACCESS_KEY = '0057ba6d7a5725c0000000002'
B2_SECRET_KEY = 'K005XvUqydtIZQvuNBYCM/UDhXfrWLQ'
BUCKET_NAME = 'ccgnimex'

# Konfigurasi Telegram
API_HASH = "aebd45c2c14b36c2c91dec3cf5e8ee9a"
APP_ID = 7120601
BOT_TOKEN = "5674151043:AAHHhHgn39e4KkXlqwxUrrtacZibMD5p558"

bot = Client("b2_uploader_bot", api_id=APP_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Buat antrian global
task_queue = asyncio.Queue()

# Coroutine untuk memproses antrian
async def process_queue():
    while True:
        task = await task_queue.get()
        logging.info("Memulai tugas dari antrian...")
        try:
            await task()
        except Exception as e:
            logging.error(f"Error saat memproses tugas: {e}")
        finally:
            task_queue.task_done()

# Tambahkan tugas ke antrian
async def add_task_to_queue(task):
    await task_queue.put(task)

# Unduh file dengan progres
async def download_with_progress(url, output_path, message, process_text):
    response = requests.get(url, stream=True)
    response.raise_for_status()
    total_size = int(response.headers.get('content-length', 0))
    downloaded_size = 0
    last_progress = 0

    with open(output_path, 'wb') as file:
        for data in response.iter_content(chunk_size=1024):
            file.write(data)
            downloaded_size += len(data)
            current_progress = int((downloaded_size / total_size) * 100)

            if current_progress - last_progress >= 5:  # Update setiap 5%
                progress_bar = int(current_progress / 5)
                bar = f"[{'=' * progress_bar}{' ' * (20 - progress_bar)}] {current_progress}%"
                await message.edit_text(f"{process_text}\n\n{bar}")
                last_progress = current_progress

def upload_file_to_s3(file_path, bucket_name, object_name):
    """
    Unggah file ke Backblaze B2 menggunakan boto3.
    """
    s3_client = boto3.client(
        's3',
        endpoint_url=B2_ENDPOINT_URL,
        aws_access_key_id=B2_ACCESS_KEY,
        aws_secret_access_key=B2_SECRET_KEY,
    )

    # Dapatkan Content-Type dari file
    content_type, _ = mimetypes.guess_type(file_path)
    if not content_type:
        mime = magic.Magic(mime=True)
        content_type = mime.from_file(file_path)
        logging.info(f"Fallback MIME Type untuk {file_path}: {content_type}")
    else:
        logging.info(f"Deteksi Content-Type untuk {file_path}: {content_type}")

    # Pastikan nama file tetap asli
    logging.info(f"Nama file untuk diunggah (asli): {repr(object_name)}")

    try:
        extra_args = {'ContentType': content_type} if content_type else {}
        s3_client.upload_file(file_path, bucket_name, object_name, ExtraArgs=extra_args)

        # Buat URL menggunakan nama file asli
        custom_domain = "https://file.ccgnimex.my.id/file"
        s3_url = f"{custom_domain}/{bucket_name}/{quote(object_name)}"

        logging.info(f"URL file yang dihasilkan: {s3_url}")
        return s3_url
    except (NoCredentialsError, PartialCredentialsError) as e:
        logging.error("Kredensial tidak valid: %s", e)
        raise
    except Exception as e:
        logging.error("Gagal mengunggah file: %s", e)
        raise



# Handler untuk /start
@bot.on_message(filters.command("start"))
async def start_handler(client, message):
    logging.info("Menerima command /start dari pengguna.")
    await message.reply(
        "Halo! Pilih opsi:\n\n"
        "1️⃣ Kirim `/file URL` untuk mengunggah file tunggal.\n"
        "2️⃣ Kirim `/archive URL` untuk mengunggah isi arsip ZIP/RAR."
    )

@bot.on_message(filters.command("archive"))
async def handle_archive(client, message):
    logging.info("Command /archive diterima")
    async def process_task():
        try:
            # Pisahkan link dan nama arsip dari pesan
            params = message.text.split(" ", 1)
            if len(params) < 2 or "|" not in params[1]:
                await message.reply("Format tidak valid. Gunakan: `/archive link | nama_arsip.zip/rar`")
                return

            archive_url, archive_name = map(str.strip, params[1].split("|", 1))

            if not archive_url or not archive_name:
                await message.reply("Link atau nama arsip tidak boleh kosong.")
                return

            # Tentukan jalur file sementara
            temp_dir = os.path.join("temp", "archive")
            os.makedirs(temp_dir, exist_ok=True)

            archive_path = os.path.join(temp_dir, archive_name)

            progress_message = await message.reply("Mengunduh arsip dari URL langsung...")

            # Unduh arsip langsung
            await download_with_progress(archive_url, archive_path, progress_message, "Mengunduh arsip")

            await progress_message.edit_text("Mengekstrak arsip...")

            # Ekstrak arsip
            extracted_dir = os.path.join(temp_dir, "extracted")
            os.makedirs(extracted_dir, exist_ok=True)

            if archive_name.endswith(".zip"):
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    zip_ref.extractall(extracted_dir)
            elif archive_name.endswith(".rar"):
                with rarfile.RarFile(archive_path, 'r') as rar_ref:
                    rar_ref.extractall(extracted_dir)
            else:
                await progress_message.edit_text("Format arsip tidak didukung. Hanya .zip dan .rar yang didukung.")
                return

            # Unggah semua file dalam folder ekstrak
            await progress_message.edit_text("Mengunggah file hasil ekstrak ke B2...")
            upload_urls = []
            for root, _, files in os.walk(extracted_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    relative_path = os.path.relpath(file_path, extracted_dir)
                    url = upload_file_to_s3(file_path, BUCKET_NAME, relative_path)
                    upload_urls.append(f"- {url}")

            # Hapus file sementara
            os.remove(archive_path)
            for root, _, files in os.walk(temp_dir, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                os.rmdir(root)

            await progress_message.edit_text(f"Semua file berhasil diunggah!\n\n" + "\n".join(upload_urls))
        except Exception as e:
            await message.reply(f"Terjadi kesalahan: {e}")
        finally:
            if os.path.exists("temp"):
                for root, _, files in os.walk("temp", topdown=False):
                    for name in files:
                        os.remove(os.path.join(root, name))
                os.rmdir("temp")

    await add_task_to_queue(process_task)


@bot.on_message(filters.command("gdrivefile"))
async def handle_gdrivefile(client, message):
    logging.info("Command /gdrivefile diterima")
    async def process_task():
        try:
            # Pisahkan link dan nama file dari pesan
            params = message.text.split(" ", 1)
            if len(params) < 2 or "|" not in params[1]:
                await message.reply("Format tidak valid. Gunakan: `/gdrivefile link_google_drive | nama_file`")
                return

            drive_link, file_name = map(str.strip, params[1].split("|", 1))

            if not drive_link or not file_name:
                await message.reply("Link atau nama file tidak boleh kosong.")
                return

            # Konversi link jika perlu
            drive_link = convert_drive_link(drive_link)
            logging.info(f"Link Google Drive setelah konversi: {drive_link}")

            # Dapatkan ID file dari link Google Drive
            file_id = None
            match = re.search(r"id=([a-zA-Z0-9_-]+)", drive_link)
            if match:
                file_id = match.group(1)
            else:
                await message.reply("Link Google Drive tidak valid.")
                return

            # Tentukan jalur file sementara
            temp_file_path = os.path.join("temp", file_name)
            os.makedirs("temp", exist_ok=True)

            progress_message = await message.reply("Mengunduh file dari Google Drive...")

            # Unduh menggunakan gdown
            gdown.download(f"https://drive.google.com/uc?id={file_id}", temp_file_path, quiet=False)

            await progress_message.edit_text("Mengunggah file ke B2...")
            url = upload_file_to_s3(temp_file_path, BUCKET_NAME, file_name)

            os.remove(temp_file_path)

            await progress_message.edit_text(f"File berhasil diunggah!\n\nURL: {url}")
        except Exception as e:
            await message.reply(f"Terjadi kesalahan: {e}")
        finally:
            if os.path.exists("temp"):
                for root, _, files in os.walk("temp", topdown=False):
                    for name in files:
                        os.remove(os.path.join(root, name))
                os.rmdir("temp")

    await add_task_to_queue(process_task)

@bot.on_message(filters.command("indexarc"))
async def handle_indexarc(client, message):
    logging.info("Command /indexarc diterima")

    async def process_task():
        temp_dir = None
        try:
            # Parsing parameter
            params = message.text.split(" ", 1)
            if len(params) < 2 or "|" not in params[1]:
                await message.reply("Format: /indexarc link | nama_file | animeid | episodelist")
                return

            parts = params[1].split("|")
            if len(parts) < 4:
                await message.reply("Parameter kurang! Format: link | nama_file | animeid | episodelist")
                return

            archive_link, archive_name, anime_id, episodelist = map(str.strip, parts)

            # Validasi input
            if not all([archive_link, archive_name, anime_id, episodelist]):
                await message.reply("Semua parameter wajib diisi!")
                return

            if not re.match(r"^\d+-\d+$", episodelist):
                await message.reply("Format episode salah! Gunakan 1-20")
                return

            start_ep, end_ep = map(int, episodelist.split("-"))
            if start_ep >= end_ep:
                await message.reply("Range episode tidak valid!")
                return

            # Setup direktori temporary
            temp_dir = os.path.join("temp", "indexarc", str(message.chat.id))
            os.makedirs(temp_dir, exist_ok=True)
            archive_path = os.path.join(temp_dir, archive_name)

            # Download dengan gdown
            progress_message = await message.reply("🔄 Mengunduh arsip...")
            gdown.download(archive_link, archive_path, quiet=False)
            await progress_message.edit_text("✅ Unduhan selesai. Mengekstrak arsip...")

            # Ekstraksi arsip
            extracted_dir = os.path.join(temp_dir, "extracted")
            os.makedirs(extracted_dir, exist_ok=True)

            if archive_name.endswith(".zip"):
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    zip_ref.extractall(extracted_dir)
            elif archive_name.endswith(".rar"):
                with rarfile.RarFile(archive_path, 'r') as rar_ref:
                    rar_ref.extractall(extracted_dir)
            else:
                await progress_message.edit_text("❌ Format arsip tidak didukung. Gunakan .zip atau .rar.")
                return

            # Proses file
            await progress_message.edit_text("🔍 Memproses file...")
            valid_ext = ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm']
            files = [
                os.path.join(root, fn)
                for root, _, filenames in os.walk(extracted_dir)
                for fn in filenames if os.path.splitext(fn)[1].lower() in valid_ext
            ]
            files.sort()

            renamed_files = []
            for idx, old_path in enumerate(files, 1):
                ext = os.path.splitext(old_path)[1]
                new_path = os.path.join(os.path.dirname(old_path), f"{idx:02d}{ext}")
                os.rename(old_path, new_path)
                renamed_files.append(new_path)

            # Upload ke S3 dan insert ke database
            await progress_message.edit_text("☁️ Mengupload ke server...")
            episode_numbers = list(range(start_ep, end_ep + 1))

            for ep_num, file_path in zip(episode_numbers, renamed_files):
                try:
                    relative_path = os.path.relpath(file_path, extracted_dir)
                    video_url = upload_file_to_s3(file_path, BUCKET_NAME, relative_path)
                    insert_into_sql(anime_id, ep_num, f"Episode {ep_num}", video_url)
                except Exception as e:
                    logging.error(f"Gagal proses episode {ep_num}: {str(e)}")

            # Kirim notifikasi ke server
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://ccgnimex.my.id/v2/android/scrapping/index.php",
                    data={"anime_id": anime_id}
                )

            await progress_message.edit_text("✅ Berhasil diproses!")
            await message.reply(f"**Anime ID {anime_id}** berhasil diindex!\nTotal episode: {len(renamed_files)}")

        except Exception as e:
            logging.exception("Error in indexarc:")
            await message.reply(f"❌ Error: {str(e)}")
        finally:
            # Cleanup
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    await add_task_to_queue(process_task)

        

@bot.on_message(filters.command("gdrivearc"))
async def handle_gdrivearc(client, message):
    logging.info("Command /gdrivearc diterima")

    async def process_task():
        try:
            # Parsing parameter dari pesan
            params = message.text.split(" ", 1)
            if len(params) < 2 or "|" not in params[1]:
                await message.reply("Format tidak valid. Gunakan: /gdrivearc link_google_drive | nama_file.zip/rar | animeid | episodelist")
                return

            parts = params[1].split("|")
            if len(parts) < 4:
                await message.reply("Parameter tidak lengkap. Pastikan menggunakan format: /gdrivearc link | nama_file | animeid | episodelist")
                return

            drive_link, archive_name, anime_id, episodelist = map(str.strip, parts)

            if not drive_link or not archive_name or not anime_id or not episodelist:
                await message.reply("Parameter tidak boleh kosong.")
                return

            # Validasi format episodelist
            if not re.match(r"^\d+-\d+$", episodelist):
                await message.reply("Format episodelist tidak valid. Gunakan format seperti 1-20. Input angka tunggal tidak diperbolehkan.")
                return

            # Konversi episodelist menjadi rentang angka
            start_episode, end_episode = map(int, episodelist.split("-"))

            if start_episode > end_episode:
                await message.reply("Awal episodelist harus lebih kecil atau sama dengan akhir episodelist. Contoh: 1-20.")
                return

            episode_numbers = list(range(start_episode, end_episode + 1))

            # Konversi link jika perlu
            drive_link = convert_drive_link(drive_link)
            logging.info(f"Link Google Drive setelah konversi: {drive_link}")

            # Dapatkan ID file dari link Google Drive
            file_id = re.search(r"id=([a-zA-Z0-9_-]+)", drive_link).group(1)

            # Tentukan jalur file sementara
            temp_dir = os.path.join("temp", "archive")
            os.makedirs(temp_dir, exist_ok=True)

            archive_path = os.path.join(temp_dir, archive_name)

            progress_message = await message.reply("Mengunduh arsip dari Google Drive...")

            # Unduh arsip menggunakan gdown
            gdown.download(f"https://drive.google.com/uc?id={file_id}", archive_path, quiet=False)

            await progress_message.edit_text("Mengekstrak arsip...")

            # Ekstrak arsip
            extracted_dir = os.path.join(temp_dir, "extracted")
            os.makedirs(extracted_dir, exist_ok=True)

            if archive_name.endswith(".zip"):
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    zip_ref.extractall(extracted_dir)
            elif archive_name.endswith(".rar"):
                with rarfile.RarFile(archive_path, 'r') as rar_ref:
                    rar_ref.extractall(extracted_dir)
            else:
                await progress_message.edit_text("Format arsip tidak didukung. Hanya .zip dan .rar yang didukung.")
                return

            # Filter valid video files (ignore .url, .txt, etc.)
            valid_video_extensions = ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm']

            # Validasi jumlah file yang diekstrak
            numbered_files = []
            for root, _, files in os.walk(extracted_dir):
                for file in files:
                    file_extension = os.path.splitext(file)[1].lower()
                    if file_extension in valid_video_extensions:  # Only process valid video files
                        numbered_files.append((os.path.join(root, file), file))

            num_files = len(numbered_files)

            if num_files != len(episode_numbers):
                await message.reply(f"Jumlah file yang diekstrak ({num_files}) tidak sesuai dengan episodelist ({len(episode_numbers)} episode).")
                return

            # Sort and rename file sequentially
            numbered_files.sort(key=lambda x: x[1])
            renamed_files = []
            for index, (full_path, original_name) in enumerate(numbered_files, start=1):
                file_extension = os.path.splitext(original_name)[1]
                new_name = f"{str(index).zfill(2)}{file_extension}"
                new_path = os.path.join(os.path.dirname(full_path), new_name)
                os.rename(full_path, new_path)
                renamed_files.append(new_path)

            # Masukkan data ke database
            for episode_number, file_path in zip(episode_numbers, renamed_files):
                relative_path = os.path.relpath(file_path, extracted_dir)
                video_url = upload_file_to_s3(file_path, BUCKET_NAME, relative_path)
                insert_into_sql(anime_id, episode_number, f"Episode {episode_number}", video_url)

            # Hapus file sementara
            os.remove(archive_path)
            for root, _, files in os.walk(temp_dir, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                os.rmdir(root)

            await progress_message.edit_text("Semua episode berhasil diproses dan dimasukkan ke database!")

            # Kirim data anime_id ke server
            url = "https://ccgnimex.my.id/v2/android/scrapping/index.php"
            data = {"anime_id": anime_id}

            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, data=data)
                    if response.status_code == 200:
                        await message.reply(f"Anime ID {anime_id} berhasil ditambahkan ke server!")
                    else:
                        await message.reply(f"Gagal menambahkan Anime ID {anime_id}. Server mengembalikan kode status {response.status_code}")
            except Exception as e:
                await message.reply(f"Terjadi kesalahan saat menambahkan Anime ID {anime_id}: {str(e)}")
        except Exception as e:
            await message.reply(f"Terjadi kesalahan: {e}")
        finally:
            if os.path.exists("temp"):
                for root, _, files in os.walk("temp", topdown=False):
                    for name in files:
                        os.remove(os.path.join(root, name))
                    os.rmdir(root)

    await add_task_to_queue(process_task)


@bot.on_message(filters.command("gdrivemp4"))
async def handle_gdrivemp4(client, message):
    logging.info("Command /gdrivemp4 diterima")

    async def process_task():
        try:
            # Parsing parameter dari pesan
            params = message.text.split(" ", 1)
            if len(params) < 2 or "|" not in params[1]:
                await message.reply("Format tidak valid. Gunakan: /gdrivemp4 link_google_drive | nama_file.mp4 | animeid | episodelist")
                return

            parts = params[1].split("|")
            if len(parts) < 4:
                await message.reply("Parameter tidak lengkap. Pastikan menggunakan format: /gdrivemp4 link | nama_file | animeid | episodelist")
                return

            drive_link, file_name, anime_id, episodelist = map(str.strip, parts)

            if not drive_link or not file_name or not anime_id or not episodelist:
                await message.reply("Parameter tidak boleh kosong.")
                return

            # Validasi format episodelist
            if not re.match(r"^\d+-\d+$", episodelist):
                await message.reply("Format episodelist tidak valid. Gunakan format seperti 1-20. Input angka tunggal tidak diperbolehkan.")
                return

            # Konversi episodelist menjadi rentang angka
            start_episode, end_episode = map(int, episodelist.split("-"))

            if start_episode > end_episode:
                await message.reply("Awal episodelist harus lebih kecil atau sama dengan akhir episodelist. Contoh: 1-20.")
                return

            episode_numbers = list(range(start_episode, end_episode + 1))

            # Konversi link jika perlu
            drive_link = convert_drive_link(drive_link)
            logging.info(f"Link Google Drive setelah konversi: {drive_link}")

            # Dapatkan ID file dari link Google Drive
            file_id = re.search(r"id=([a-zA-Z0-9_-]+)", drive_link).group(1)

            # Tentukan jalur file sementara
            temp_dir = os.path.join("temp", "mp4")
            os.makedirs(temp_dir, exist_ok=True)

            file_path = os.path.join(temp_dir, file_name)

            progress_message = await message.reply("Mengunduh file MP4 dari Google Drive...")

            # Unduh file MP4 menggunakan gdown
            gdown.download(f"https://drive.google.com/uc?id={file_id}", file_path, quiet=False)

            # Pastikan file terunduh dengan ekstensi .mp4
            if not file_name.endswith(".mp4"):
                await message.reply("Format file tidak valid. Hanya file .mp4 yang didukung.")
                return

            # Masukkan data ke database
            for episode_number in episode_numbers:
                relative_path = os.path.relpath(file_path, temp_dir)
                video_url = upload_file_to_s3(file_path, BUCKET_NAME, f"{anime_id}/Episode_{episode_number}.mp4")
                insert_into_sql(anime_id, episode_number, f"Episode {episode_number}", video_url)

            # Hapus file sementara
            os.remove(file_path)
            os.rmdir(temp_dir)

            await progress_message.edit_text("File MP4 berhasil diproses dan dimasukkan ke database!")

            # Kirim data anime_id ke server
            url = "https://ccgnimex.my.id/v2/android/scrapping/index.php"
            data = {"anime_id": anime_id}

            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, data=data)
                    if response.status_code == 200:
                        await message.reply(f"Anime ID {anime_id} berhasil ditambahkan ke server!")
                    else:
                        await message.reply(f"Gagal menambahkan Anime ID {anime_id}. Server mengembalikan kode status {response.status_code}")
            except Exception as e:
                await message.reply(f"Terjadi kesalahan saat menambahkan Anime ID {anime_id}: {str(e)}")
        except Exception as e:
            await message.reply(f"Terjadi kesalahan: {e}")
        finally:
            if os.path.exists("temp"):
                for root, _, files in os.walk("temp", topdown=False):
                    for name in files:
                        os.remove(os.path.join(root, name))
                    os.rmdir(root)

    await add_task_to_queue(process_task)


@bot.on_message(filters.command("indexmp4"))
async def handle_indexmp4(client, message):
    logging.info("Command /indexmp4 diterima")

    async def process_task():
        try:
            # Parsing parameter dari pesan
            params = message.text.split(" ", 1)
            if len(params) < 2 or "|" not in params[1]:
                await message.reply("Format tidak valid. Gunakan: /indexmp4 link_index | nama_file.mp4 | animeid | episodelist")
                return

            parts = params[1].split("|")
            if len(parts) < 4:
                await message.reply("Parameter tidak lengkap. Pastikan menggunakan format: /indexmp4 link | nama_file | animeid | episodelist")
                return

            index_link, file_name, anime_id, episodelist = map(str.strip, parts)

            if not index_link or not file_name or not anime_id or not episodelist:
                await message.reply("Parameter tidak boleh kosong.")
                return

            # Validasi format episodelist
            if not re.match(r"^\d+-\d+$", episodelist):
                await message.reply("Format episodelist tidak valid. Gunakan format seperti 1-20. Input angka tunggal tidak diperbolehkan.")
                return

            # Konversi episodelist menjadi rentang angka
            start_episode, end_episode = map(int, episodelist.split("-"))

            if start_episode > end_episode:
                await message.reply("Awal episodelist harus lebih kecil atau sama dengan akhir episodelist. Contoh: 1-20.")
                return

            episode_numbers = list(range(start_episode, end_episode + 1))

            # Tentukan jalur file sementara
            temp_dir = os.path.join("temp", "mp4")
            os.makedirs(temp_dir, exist_ok=True)

            file_path = os.path.join(temp_dir, file_name)

            progress_message = await message.reply("Mengunduh file MP4 dari link index...")

            # Unduh file MP4 menggunakan httpx
            async with httpx.AsyncClient() as client:
                response = await client.get(index_link)
                if response.status_code == 200:
                    with open(file_path, "wb") as f:
                        f.write(response.content)
                else:
                    await progress_message.edit_text("Gagal mengunduh file. Pastikan link index valid.")
                    return

            # Pastikan file terunduh dengan ekstensi .mp4
            if not file_name.endswith(".mp4"):
                await message.reply("Format file tidak valid. Hanya file .mp4 yang didukung.")
                return

            # Masukkan data ke database
            await progress_message.edit_text("Memproses file dan memasukkan ke database...")
            for episode_number in episode_numbers:
                # Menggunakan file_path yang ada, bukan duplikat
                relative_path = os.path.relpath(file_path, temp_dir)
                video_url = upload_file_to_s3(file_path, BUCKET_NAME, f"{anime_id}/Episode_{episode_number}.mp4")
                insert_into_sql(anime_id, episode_number, f"Episode {episode_number}", video_url)

            # Hapus file sementara
            os.remove(file_path)
            os.rmdir(temp_dir)

            await progress_message.edit_text("File MP4 berhasil diproses dan dimasukkan ke database!")

            # Kirim data anime_id ke server
            url = "https://ccgnimex.my.id/v2/android/scrapping/index.php"
            data = {"anime_id": anime_id}

            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, data=data)
                    if response.status_code == 200:
                        await message.reply(f"Anime ID {anime_id} berhasil ditambahkan ke server!")
                    else:
                        await message.reply(f"Gagal menambahkan Anime ID {anime_id}. Server mengembalikan kode status {response.status_code}")
            except Exception as e:
                await message.reply(f"Terjadi kesalahan saat menambahkan Anime ID {anime_id}: {str(e)}")
        except Exception as e:
            await message.reply(f"Terjadi kesalahan: {e}")
        finally:
            # Cleanup files
            if os.path.exists("temp"):
                for root, _, files in os.walk("temp", topdown=False):
                    for name in files:
                        os.remove(os.path.join(root, name))
                    os.rmdir(root)

    await add_task_to_queue(process_task)




def convert_drive_link(link):
    """
    Konversi berbagai format link Google Drive ke format standar:
    https://drive.google.com/uc?id={file_id}
    """
    # Coba cocokkan link dengan pola `drive.google.com`
    match = re.search(r"id=([a-zA-Z0-9_-]+)", link)
    if match:
        return f"https://drive.google.com/uc?id={match.group(1)}"
    
    # Coba cocokkan link dengan pola `drive.usercontent.google.com`
    match = re.search(r"download\?id=([a-zA-Z0-9_-]+)", link)
    if match:
        return f"https://drive.google.com/uc?id={match.group(1)}"

    # Coba cocokkan pola `/d/{file_id}/`
    match = re.search(r"/d/([a-zA-Z0-9_-]+)/", link)
    if match:
        return f"https://drive.google.com/uc?id={match.group(1)}"

    # Jika tidak cocok, kembalikan link asli
    return link


@bot.on_message(filters.command("add"))
async def add_command(client, message):
    # Extract the anime_id from the message text
    parts = message.text.split()
    if len(parts) == 2:
        anime_id = parts[1]

        # Send a POST request to the specified URL with the anime_id using httpx
        url = "https://ccgnimex.my.id/v2/android/scrapping/index.php"
        data = {"anime_id": anime_id}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, data=data)
                if response.status_code == 200:
                    await message.reply_text(f"Anime ID {anime_id} added successfully!")
                else:
                    await message.reply_text(f"Failed to add Anime ID {anime_id}. Server returned status code {response.status_code}")
        except Exception as e:
            await message.reply_text(f"An error occurred: {str(e)}")
    else:
        await message.reply_text("Invalid command format. Use: '/add <anime_id>'")

        

@bot.on_message(filters.command("file"))
async def handle_file(client, message):
    logging.info("Command /file diterima")
    async def process_task():
        try:
            # Pisahkan link dan nama file dari pesan
            params = message.text.split(" ", 1)
            if len(params) < 2 or "|" not in params[1]:
                await message.reply("Format tidak valid. Gunakan: `/file link | nama_file`")
                return

            file_url, file_name = map(str.strip, params[1].split("|", 1))

            if not file_url or not file_name:
                await message.reply("Link atau nama file tidak boleh kosong.")
                return

            temp_file_path = os.path.join("temp", file_name)

            os.makedirs("temp", exist_ok=True)

            progress_message = await message.reply("Memulai proses...")
            await download_with_progress(file_url, temp_file_path, progress_message, "Mengunduh file")

            await progress_message.edit_text("Mengunggah file ke B2...")
            url = upload_file_to_s3(temp_file_path, BUCKET_NAME, file_name)

            os.remove(temp_file_path)

            await progress_message.edit_text(f"File berhasil diunggah!\n\nURL: {url}")
        except Exception as e:
            await message.reply(f"Terjadi kesalahan: {e}")
        finally:
            if os.path.exists("temp"):
                for root, _, files in os.walk("temp", topdown=False):
                    for name in files:
                        os.remove(os.path.join(root, name))
                os.rmdir("temp")

    await add_task_to_queue(process_task)



# Main function
async def main():
    stop_event = asyncio.Event()

    def stop_event_loop(*args):
        logging.info("Signal diterima, menghentikan bot...")
        stop_event.set()

    # Tangani signal
    signal.signal(signal.SIGINT, lambda *args: asyncio.create_task(stop_event_loop(*args)))
    signal.signal(signal.SIGTERM, lambda *args: asyncio.create_task(stop_event_loop(*args)))

    try:
        await bot.start()
        logging.info("Bot berjalan. Tekan Ctrl+C untuk menghentikan.")
        asyncio.create_task(process_queue())
        await stop_event.wait()
    finally:
        await bot.stop()
        logging.info("Bot telah dihentikan.")

if __name__ == "__main__":
    import asyncio
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot dihentikan secara manual.")
