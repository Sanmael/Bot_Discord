from flask import Flask, request, jsonify
import yt_dlp
import re

import asyncio
import os
import subprocess
import threading
import tempfile

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
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt"))  # Path to cookies.txt file
YT_COOKIES_BROWSER = os.getenv("YT_COOKIES_BROWSER")  # Browser name (chrome, firefox, etc)

def get_ydl_opts(use_cookies=False):
    """Build yt-dlp options with optional cookie support"""
    opts = {
        'quiet': True,
        'no_warnings': True,
    }
    
    # Add cookies only if explicitly requested
    if use_cookies:
        if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
            opts['cookiefile'] = YT_COOKIES_FILE
        elif YT_COOKIES_BROWSER:
            opts['cookiesfrombrowser'] = (YT_COOKIES_BROWSER,)
    
    return opts

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
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        
        # Generate unique filename
        mp3_file = os.path.join(DOWNLOAD_DIR, f"{os.urandom(8).hex()}.mp3")
        
        # Try without cookies first (usually works for public videos)
        try:
            ydl_opts = get_ydl_opts(use_cookies=False)
            ydl_opts.update({
                'format': 'bestaudio/best',
                'skip_unavailable_fragments': True,
                'check_formats': False,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': mp3_file.replace('.mp3', ''),
                'ffmpeg_location': os.path.dirname(FFMPEG_PATH) if os.path.dirname(FFMPEG_PATH) else None,
            })
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            if os.path.exists(mp3_file):
                if os.path.getsize(mp3_file) > MAX_MP3_BYTES:
                    os.remove(mp3_file)
                    return False, None, "MP3 excede o limite de 100MB."
                return True, mp3_file, None
        except Exception as e:
            print(f"[DEBUG] Download sem cookies falhou: {str(e)}")
        
        # Try with cookies if available
        try:
            ydl_opts = get_ydl_opts(use_cookies=True)
            if 'cookiefile' not in ydl_opts and 'cookiesfrombrowser' not in ydl_opts:
                # No cookies available, skip this attempt
                raise Exception("Nenhum cookie disponível")
            
            ydl_opts.update({
                'format': 'bestaudio/best',
                'skip_unavailable_fragments': True,
                'check_formats': False,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': mp3_file.replace('.mp3', ''),
                'ffmpeg_location': os.path.dirname(FFMPEG_PATH) if os.path.dirname(FFMPEG_PATH) else None,
            })
            
            print("[DEBUG] Tentando com cookies...")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            if os.path.exists(mp3_file):
                if os.path.getsize(mp3_file) > MAX_MP3_BYTES:
                    os.remove(mp3_file)
                    return False, None, "MP3 excede o limite de 100MB."
                return True, mp3_file, None
        except Exception as e:
            print(f"[DEBUG] Download com cookies falhou: {str(e)}")
        
        return False, None, "Não foi possível fazer download do vídeo"

    except Exception as e:
        print(f"[DEBUG] Erro geral: {str(e)}")
        return False, None, str(e)


def get_video_info(url):
    try:
        ydl_opts = get_ydl_opts(use_cookies=False)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            video_info = {
                "title": info.get('title'),
                "author": info.get('uploader'),
                "length": info.get('duration'),
                "views": info.get('view_count'),
                "description": info.get('description'),
                "publish_date": info.get('upload_date'),
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
        ydl_opts = get_ydl_opts(use_cookies=False)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            
            resolutions = list(set([
                f.get('height', 0) 
                for f in formats 
                if f.get('height') and f.get('vcodec') != 'none'
            ]))
            
            return jsonify({
                "resolutions": sorted([f"{r}p" for r in resolutions if r > 0])
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
        "!pular - Para a reprodução atual, mas permanece conectado ao canal de voz.\n"
        "!setcookies <cookies> - [ADMIN] Atualiza os cookies do YouTube.\n"
        "!clearcookies - [ADMIN] Limpa/reseta os cookies do YouTube."
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


@bot.command(name="setcookies")
@commands.is_owner()
async def setcookies(ctx, *, cookies: str = None):
    """
    Atualiza o arquivo de cookies do YouTube.
    Uso 1 (texto): !setcookies <conteúdo dos cookies>
    Uso 2 (arquivo): !setcookies (anexar arquivo cookies.txt)
    Apenas o dono do bot pode usar este comando.
    """
    # Check for file attachment
    if ctx.message.attachments:
        try:
            attachment = ctx.message.attachments[0]
            if not attachment.filename.endswith('.txt'):
                await ctx.send("❌ Por favor, envie um arquivo .txt")
                return
            
            # Download file content
            cookies_content = await attachment.read()
            cookies_content = cookies_content.decode('utf-8')
            
            # Write to cookies file
            with open(YT_COOKIES_FILE, 'w', encoding='utf-8') as f:
                f.write(cookies_content)
            
            await ctx.send("✅ Cookies atualizados com sucesso via arquivo!")
            
            # Delete the command message for security
            try:
                await ctx.message.delete()
            except:
                pass
        except Exception as e:
            await ctx.send(f"❌ Erro ao processar arquivo: {str(e)}")
    
    # Check for text content
    elif cookies:
        try:
            # Write cookies to file
            with open(YT_COOKIES_FILE, 'w', encoding='utf-8') as f:
                f.write(cookies)
            
            await ctx.send("✅ Cookies atualizados com sucesso!")
            
            # Delete the command message for security
            try:
                await ctx.message.delete()
            except:
                pass
                
        except Exception as e:
            await ctx.send(f"❌ Erro ao atualizar cookies: {str(e)}")
    
    else:
        await ctx.send("❌ Use um dos formatos:\n`!setcookies <cookies>` ou anexe um arquivo .txt")


@bot.command(name="clearcookies")
@commands.is_owner()
async def clearcookies(ctx):
    """
    Limpa/reseta o arquivo de cookies do YouTube.
    Uso: !clearcookies
    Apenas o dono do bot pode usar este comando.
    """
    try:
        # Clear cookies file
        with open(YT_COOKIES_FILE, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n# This file is generated by yt-dlp. Do not edit.\n\n")
        
        await ctx.send("✅ Cookies foram resetados! O bot agora vai usar a configuração padrão (sem cookies).")
            
    except Exception as e:
        await ctx.send(f"❌ Erro ao limpar cookies: {str(e)}")
    
def run_flask():
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")

    bot.run(DISCORD_TOKEN)