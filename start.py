from flask import Flask, request, jsonify
from pytubefix import YouTube
import re

import asyncio
import os
import subprocess
import threading

import discord
from discord.ext import commands

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
_local_ffmpeg = os.path.join(BASE_DIR, "ffmpeg", "bin", "ffmpeg.exe")
FFMPEG_PATH = os.getenv("FFMPEG_PATH") or (_local_ffmpeg if os.path.exists(_local_ffmpeg) else "ffmpeg")
MAX_MP3_BYTES = 100 * 1024 * 1024
MP3_BITRATE = "192k"
MP3_SAMPLE_RATE = "44100"
MP3_CHANNELS = "2"

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_CLIENT = os.getenv("YOUTUBE_CLIENT", "WEB")


def build_youtube(url):
    return YouTube(url, YOUTUBE_CLIENT)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
current_tracks = {}

@app.route('/download_mp3', methods=['POST'])
def download_audio():
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({"error": "Parâmetro 'url' ausente."}), 400

    if not is_valid_youtube_url(url):
        return jsonify({"error": "URL do YouTube inválida."}), 400

    success, mp3_path, error = download_mp3(url)

    if success:
        return jsonify({"message": "MP3 baixado com sucesso."}), 200
    else:
        return jsonify({"error": error}), 500

def download_mp3(url):
    try:
        yt = build_youtube(url)
        audio_streams = list(yt.streams.filter(only_audio=True))
        if not audio_streams:
            return False, None, "Fluxo de áudio não encontrado."

        def abr_value(s):
            if not s.abr:
                return 0
            digits = re.sub(r"\D", "", s.abr)
            return int(digits) if digits else 0

        stream = min(audio_streams, key=abr_value)

        if not stream:
            return False, None, "Fluxo de áudio não encontrado."

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        downloaded_file = stream.download(output_path=DOWNLOAD_DIR)

        base, _ = os.path.splitext(downloaded_file)
        mp3_file = base + ".mp3"

        if os.path.exists(mp3_file):
            os.remove(mp3_file)

        subprocess.run([
            FFMPEG_PATH,
            "-y",
            "-i", downloaded_file,
            "-vn",
            "-ab", MP3_BITRATE,
            "-ar", MP3_SAMPLE_RATE,
            "-ac", MP3_CHANNELS,
            mp3_file
        ], check=True)

        os.remove(downloaded_file)

        if os.path.getsize(mp3_file) > MAX_MP3_BYTES:
            os.remove(mp3_file)
            return False, None, "MP3 excede o limite de 100MB."

        return True, mp3_file, None

    except Exception as e:
        return False, None, str(e)


def get_video_info(url):
    try:
        yt = build_youtube(url)
        stream = yt.streams.first()
        video_info = {
            "title": yt.title,
            "author": yt.author,
            "length": yt.length,
            "views": yt.views,
            "description": yt.description,
            "publish_date": yt.publish_date,
        }
        return video_info, None
    except Exception as e:
        return None, str(e)

def is_valid_youtube_url(url):
    pattern = r"^(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)[\w-]+(\?\S*)?(&\S*)?|youtu\.be/[\w-]+(\?\S*)?)$"
    return re.match(pattern, url) is not None


@app.route('/video_info', methods=['POST'])
def video_info():
    data = request.get_json()
    url = data.get('url')
    
    if not url:
        return jsonify({"error": "Parâmetro 'url' ausente."}), 400

    if not is_valid_youtube_url(url):
        return jsonify({"error": "URL do YouTube inválida."}), 400
    
    video_info, error_message = get_video_info(url)
    
    if video_info:
        return jsonify(video_info), 200
    else:
        return jsonify({"error": error_message}), 500


@app.route('/available_resolutions', methods=['POST'])
def available_resolutions():
    data = request.get_json()
    url = data.get('url')
    
    if not url:
        return jsonify({"error": "Parâmetro 'url' ausente."}), 400

    if not is_valid_youtube_url(url):
        return jsonify({"error": "URL do YouTube inválida."}), 400
    
    try:
        yt = build_youtube(url)
        progressive_resolutions = list(set([
            stream.resolution 
            for stream in yt.streams.filter(progressive=True, file_extension='mp4')
            if stream.resolution
        ]))
        all_resolutions = list(set([
            stream.resolution 
            for stream in yt.streams.filter(file_extension='mp4')
            if stream.resolution
        ]))
        return jsonify({
            "progressive": sorted(progressive_resolutions),
            "all": sorted(all_resolutions)
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


async def ensure_voice(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Você precisa estar em um canal de voz.")
        return None

    channel = ctx.author.voice.channel
    if ctx.voice_client:
        if ctx.voice_client.channel != channel:
            await ctx.voice_client.move_to(channel)
    else:
        await channel.connect()
    return ctx.voice_client


def cleanup_track(guild_id):
    path = current_tracks.pop(guild_id, None)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def cleanup_downloads_dir():
    if not os.path.isdir(DOWNLOAD_DIR):
        return
    for name in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user}")


@bot.command(name="ajuda")
async def ajuda(ctx):
    help_message = (
        "Comandos disponíveis:\n"
        "!tocar <URL do YouTube> - Toca o áudio do vídeo no canal de voz.\n"
        "!parar - Para a reprodução e desconecta do canal de voz.\n"
        "!pausar - Pausa a reprodução atual.\n"
        "!continuar - Continua a reprodução pausada.\n"
        "!pular - Para a reprodução atual, mas permanece conectado ao canal de voz."
    )
    await ctx.send(help_message)

@bot.command(name="continuar")
async def continuar(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_paused():
        await ctx.send("Nada está pausado.")
        return

    ctx.voice_client.resume()
    await ctx.send("Continuando.")

@bot.command(name="pausar")
async def pausar(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("Nada está tocando.")
        return

    ctx.voice_client.pause()
    await ctx.send("Pausado.")

@bot.command(name="tocar")
async def tocar(ctx, url: str):
    if not is_valid_youtube_url(url):
        await ctx.send("URL do YouTube inválida.")
        return

    voice_client = await ensure_voice(ctx)
    if not voice_client:
        return

    await ctx.send("Baixando a droga do áudio...")
    cleanup_downloads_dir()
    loop = asyncio.get_running_loop()
    success, mp3_path, error = await loop.run_in_executor(None, download_mp3, url)

    if not success:
        await ctx.send(f"Falha ao baixar: {error}")
        return

    if voice_client.is_playing():
        voice_client.stop()

    cleanup_track(ctx.guild.id)
    current_tracks[ctx.guild.id] = mp3_path

    audio = discord.FFmpegPCMAudio(mp3_path, executable=FFMPEG_PATH)

    def after_play(err):
        cleanup_track(ctx.guild.id)
        if err:
            print(f"Playback error: {err}")
        asyncio.run_coroutine_threadsafe(voice_client.disconnect(), bot.loop)

    voice_client.play(audio, after=after_play)
    await ctx.send("Tocando agora.")


@bot.command(name="parar")
async def parar(ctx):
    if not ctx.voice_client:
        await ctx.send("Não conectado a um canal de voz.")
        return

    ctx.voice_client.stop()
    cleanup_track(ctx.guild.id)
    await ctx.voice_client.disconnect()
    await ctx.send("Parado e limpo.")


@bot.command(name="pular")
async def pular(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("Nada está tocando.")
        return

    ctx.voice_client.stop()
    cleanup_track(ctx.guild.id)
    await ctx.send("Pulou e limpo.")
    
def run_flask():
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")

    bot.run(DISCORD_TOKEN)